import json
import re
import shutil
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import frappe
from frappe.utils import now_datetime
from tap_lms.onboarding.utils import authenticate_api_key
from tap_lms.utils.bigquery_helper import get_bigquery_client, run_query
from tap_lms.utils.gcs_client import get_gcs_bucket
from tap_lms.utils.scheduled_jobs import get_job_config

SCHEDULED_JOB_NAME = "teacher_reporting.run_weekly_extract"

LOCK_STALE_MINUTES = 10
STUDENT_CHUNK_SIZE = 5000
SCHOOL_CHUNK_SIZE = 5000
DISTRICT_CHUNK_SIZE = 5000

_LEGACY_STAGING_DIRNAME = ".staging/teacher_reporting"
_LEGACY_LIVE_DIRNAME = "live/teacher_reporting"
_LEGACY_LIVE_PARENT = "live"
_LEGACY_PREVIOUS_DIRNAME = "teacher_reporting.previous"
_LEGACY_MISSING_FILENAME = "missing.jsonl"

MANIFEST_FILENAME = "pdf_manifest.json"

HARD_MAX_WORKERS = 32
HARD_MAX_BATCH_SIZE = 2000
HARD_MAX_SIGNED_URL_TTL_MINUTES = 10080

REQUIRED_SETTING_FIELDS = ("dataset_id", "contacts_table", "messages_table", "gcs_settings_name")

_SCHOOL_ID_PATTERNS = (
    re.compile(r"^SC\d{8}$"),
    re.compile(r"^[A-Z]{2}[A-Z]{2}[A-Z0-9]{2}$"),
)
_MAX_SCHOOL_ID_LEN_IN_LOG = 80

_STUDENT_ID_PATTERN = re.compile(r"^ST\d{8}$")


def _normalize_school_id(value):
    return (value or "").strip().upper()


def _looks_like_school_id(value):
    if not value:
        return False
    if "\n" in value or "\r" in value:
        return False
    return any(p.fullmatch(value) for p in _SCHOOL_ID_PATTERNS)


def _looks_like_student_id(value):
    if not value:
        return False
    if "\n" in value or "\r" in value:
        return False
    return bool(_STUDENT_ID_PATTERN.fullmatch(value))


def get_reporting_settings():
    job = get_job_config(SCHEDULED_JOB_NAME)
    parameters = job.parameters
    return frappe._dict({
        "enabled": job.enabled,
        **parameters,
        "parquet_dir": frappe.get_site_path("private", "files", "teacher_reporting"),
        "max_workers": min(parameters.max_workers, HARD_MAX_WORKERS),
        "batch_size": min(parameters.batch_size, HARD_MAX_BATCH_SIZE),
        "signed_url_ttl_minutes": min(parameters.signed_url_ttl_minutes, HARD_MAX_SIGNED_URL_TTL_MINUTES),
    })


def _required_fields_ok(settings):
    missing = [f for f in REQUIRED_SETTING_FIELDS if not settings.get(f)]
    return (not missing), missing


QUERY_TEMPLATE = """
WITH students AS (
    SELECT
        JSON_EXTRACT_SCALAR(raw_fields, '$.student_id.value') AS student_id,
        JSON_EXTRACT_SCALAR(raw_fields, '$.grade.value')      AS grade,
        TRIM(JSON_EXTRACT_SCALAR(raw_fields, '$.school_id.value')) AS school_id,
        phone
    FROM `{contacts_table_fq}`
    WHERE TRUE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY updated_at DESC) = 1
        AND REGEXP_CONTAINS(group_labels, @group_label_pattern)
),
activity AS (
    SELECT
        contact_phone AS phone,
        MAX(IF(flow_label LIKE @watched_flow_label, 1, 0)) AS watched,
        MAX(IF(flow_label LIKE @shared_flow_label, 1, 0))  AS shared
    FROM `{messages_table_fq}`
    WHERE flow_name LIKE @main_flow_name
      AND (flow_label LIKE @watched_flow_label OR flow_label LIKE @shared_flow_label)
      AND DATE(inserted_at) BETWEEN @window_start AND @window_end
    GROUP BY contact_phone
)
SELECT
    students.student_id         AS student_id,
    students.grade               AS grade,
    students.school_id           AS school_id,
    IFNULL(activity.watched, 0)  AS watched_video,
    IFNULL(activity.shared, 0)   AS shared_work
FROM students
LEFT JOIN activity USING (phone)
WHERE students.school_id IS NOT NULL AND students.school_id != ''
  AND students.student_id IS NOT NULL AND students.student_id != ''
ORDER BY students.school_id, students.student_id
"""


def _last_week_window(ref_dt):
    end = ref_dt.date() - timedelta(days=1)
    start = end - timedelta(days=6)
    return start, end


def _run_bq_query(settings, start, end, client=None):
    import pandas as pd

    client = client or get_bigquery_client()
    query = QUERY_TEMPLATE.format(
        contacts_table_fq=f"{client.project}.{settings.dataset_id}.{settings.contacts_table}",
        messages_table_fq=f"{client.project}.{settings.dataset_id}.{settings.messages_table}",
    )
    params = [
        ("group_label_pattern", "STRING", settings.group_label_pattern),
        ("main_flow_name", "STRING", settings.main_flow_name),
        ("watched_flow_label", "STRING", settings.watched_flow_label),
        ("shared_flow_label", "STRING", settings.shared_flow_label),
        ("window_start", "DATE", start),
        ("window_end", "DATE", end),
    ]
    rows = run_query(
        client,
        query,
        query_parameters=params,
        maximum_bytes_billed=int(settings.max_query_gb) * (1024 ** 3),
    )
    df = rows.to_dataframe(create_bqstorage_client=False)
    if df.empty:
        df = pd.DataFrame(columns=["student_id", "grade", "school_id", "watched_video", "shared_work"])
    df["student_id"] = df["student_id"].astype(str)
    df["school_id"] = df["school_id"].astype(str)
    df["grade"] = df["grade"].astype(str)
    df["watched_video"] = df["watched_video"].fillna(0).astype(int)
    df["shared_work"] = df["shared_work"].fillna(0).astype(int)
    return df


_NAME_WHITESPACE_RE = re.compile(r"\s+")
_NAME_LETTER_RUN_RE = re.compile(r"[A-Za-z]+")
_MAX_NAME_LEN = 120


def _titlecase_name(name):
    try:
        name = str(name) if name is not None else ""
    except Exception:
        return ""
    name = name.strip()
    if not name:
        return ""
    if len(name) > _MAX_NAME_LEN:
        name = name[:_MAX_NAME_LEN]
    name = _NAME_WHITESPACE_RE.sub(" ", name).strip()

    def _cap_letter_run(m):
        part = m.group(0)
        return part[:1].upper() + part[1:].lower()

    return _NAME_LETTER_RUN_RE.sub(_cap_letter_run, name)


def _resolve_student_names_bulk(student_ids):
    seen = set()
    clean_ids = []
    for sid in student_ids:
        sid = (sid or "").strip()
        if sid and sid not in seen and _looks_like_student_id(sid):
            seen.add(sid)
            clean_ids.append(sid)

    if not clean_ids:
        return {}

    resolved = {}
    for i in range(0, len(clean_ids), STUDENT_CHUNK_SIZE):
        chunk = clean_ids[i:i + STUDENT_CHUNK_SIZE]
        # NOTE: Student.name is the autonamed primary key (format: "ST.########"),
        # NOT the mostly-empty `profile_id` field (only ~15/670k rows had it set).
        # BigQuery's student_id values correspond to Student.name.
        rows = frappe.get_all(
            "Student",
            filters={"name": ["in", chunk]},
            fields=["name", "name1"],
        )
        for row in rows:
            if not row.name:
                continue
            clean_name = _titlecase_name(row.name1)
            if clean_name:
                resolved[row.name] = clean_name
    return resolved


def _attach_resolved_names(df, name_map):
    df["student_name"] = df["student_id"].map(name_map).fillna("")
    return df


def _validate_and_normalize(df):
    df = df[(df["school_id"].str.strip() != "") & (df["student_id"].str.strip() != "")].copy()
    df["school_id"] = df["school_id"].map(_normalize_school_id)
    return df


def _resolve_school_names_bulk(school_ids):
    seen = set()
    clean_ids = []
    for sid in school_ids:
        sid = _normalize_school_id(sid)
        if sid and sid not in seen and _looks_like_school_id(sid):
            seen.add(sid)
            clean_ids.append(sid)

    if not clean_ids:
        return {}

    school_rows = []
    for i in range(0, len(clean_ids), SCHOOL_CHUNK_SIZE):
        chunk = clean_ids[i:i + SCHOOL_CHUNK_SIZE]
        # NOTE: School.name is the autonamed primary key (format:
        # "{name1}-SC{#####}", i.e. values like "SC00000054"), NOT the
        # `keyword` field (a separate slug like "tapschool:gpsn8100").
        # BigQuery's school_id values correspond to School.name.
        school_rows.extend(frappe.get_all(
            "School",
            filters={"name": ["in", chunk]},
            fields=["name", "name1", "district", "state"],
        ))

    by_keyword = {}
    for row in school_rows:
        kw = _normalize_school_id(row.name)
        if kw and kw not in by_keyword:
            by_keyword[kw] = row

    districts_needing_state = sorted({
        row.district for row in by_keyword.values()
        if row.district and not (row.state or "").strip()
    })
    district_state = {}
    for i in range(0, len(districts_needing_state), DISTRICT_CHUNK_SIZE):
        chunk = districts_needing_state[i:i + DISTRICT_CHUNK_SIZE]
        for row in frappe.get_all(
            "District",
            filters={"name": ["in", chunk]},
            fields=["name", "state"],
        ):
            district_state[row.name] = (row.state or "").strip()

    resolved = {}
    for sid in clean_ids:
        row = by_keyword.get(sid)
        if not row:
            continue
        school_name = (row.name1 or "").strip()
        if not school_name:
            continue
        state_name = (row.state or "").strip()
        district_name = row.district or ""
        if district_name and not state_name:
            state_name = district_state.get(district_name, "")
        resolved[sid] = (school_name, district_name, state_name)
    return resolved


def _clean_school_display_name(name):
    return re.sub(r"\s+", " ", (name or "")).strip()


def _compute_school_aggregates(df):
    total = int(len(df))
    watched = int(df["watched_video"].sum()) if total else 0
    shared = int(df["shared_work"].sum()) if total else 0
    return {"watched": watched, "not_watched": total - watched, "shared": shared, "total": total}


def _grade_sort_key(grade_label):
    digits = re.match(r"^\d+", grade_label or "")
    return (0, int(digits.group())) if digits else (1, grade_label or "")


def _group_by_grade(df):
    df = df[df["student_name"] != ""].copy()
    if df.empty:
        return []

    both = df["watched_video"].astype(bool) & df["shared_work"].astype(bool)
    either = df["watched_video"].astype(bool) | df["shared_work"].astype(bool)
    df["_status_rank"] = 2
    df.loc[either, "_status_rank"] = 1
    df.loc[both, "_status_rank"] = 0

    df = df.sort_values(["_status_rank", "student_name"], kind="stable")

    groups = []
    for grade_label, chunk in df.groupby("grade", sort=False):
        chunk = chunk.drop(columns="_status_rank")
        groups.append({
            "grade": grade_label,
            "students": chunk.to_dict("records"),
            "total": len(chunk),
            "watched": int(chunk["watched_video"].sum()),
            "shared": int(chunk["shared_work"].sum()),
        })
    groups.sort(key=lambda g: _grade_sort_key(g["grade"]))
    return groups


def _format_date_range_label(start, end):
    start_label = f"{start.day} {start.strftime('%b')}"
    end_label = f"{end.day} {end.strftime('%b')} {end.year}"
    return f"{start_label} - {end_label}"


_PDF_TEMPLATE = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
@page { size: A4; margin: 12mm 12mm; }
* { box-sizing: border-box; }
body {
  font-family: "DejaVu Sans", Arial, sans-serif;
  color: #1C1C1C;
  font-size: 12px;
}
.banner-school {
  background: #145C3A;
  color: #FFFFFF;
  font-size: 16px;
  font-weight: 700;
  text-align: center;
  padding: 8px 14px 6px;
}
.banner-district {
  background: #145C3A;
  color: #FFFFFF;
  font-size: 10.5px;
  font-weight: 700;
  text-align: center;
  padding: 4px 14px 7px;
}
.banner-range {
  background: #FFC928;
  color: #1C1C1C;
  font-size: 12px;
  font-weight: 700;
  text-align: center;
  padding: 6px 14px;
  margin-bottom: 14px;
  letter-spacing: 0.2px;
}
.gradehdr {
  background: #145C3A;
  padding: 7px 12px;
  font-weight: 700;
  font-size: 12px;
  color: #FFFFFF;
}
table {
  width: 100%;
  border-collapse: collapse;
  border: 1px solid #D9D9D9;
  border-top: none;
  margin-bottom: 10px;
}
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
th, td {
  text-align: center;
  padding: 6px 12px;
  font-size: 11.5px;
}
th {
  background: #EAF3ED;
  color: #145C3A;
  font-weight: 700;
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  border-bottom: 1px solid #D9D9D9;
}
td { border-bottom: 1px solid #D9D9D9; }
td.name-col { width: 55%; text-align: left; color: #1C1C1C; font-weight: 600; }
td.mark-col { width: 22.5%; }
.icon-yes, .icon-no { font-weight: 700; font-size: 13px; }
.icon-yes { color: #145C3A; }
.icon-no { color: #A33A34; }
.row-both td { background: #EAF3ED; }
.row-partial td { background: #FFF6D9; }
.footer {
  margin-top: 16px;
  padding-top: 7px;
  border-top: 1px solid #D9D9D9;
  display: flex;
  justify-content: space-between;
  font-size: 9px;
  color: #777;
}
</style></head>
<body>

<div class="banner-school">{{ school_name }}</div>
{% if district_name or state_name %}
<div class="banner-district">{{ district_name }}{% if district_name and state_name %}, {% endif %}{{ state_name }}</div>
{% endif %}
<div class="banner-range">{{ date_range_label }}</div>

{% for g in groups %}
<div class="gradehdr">Grade {{ g.grade }}</div>
<table>
  <thead>
    <tr><th>Student Name</th><th>TAP Video</th><th>TAP Work</th></tr>
  </thead>
  <tbody>
    {% for s in g.students %}
    <tr class="{% if s.watched_video and s.shared_work %}row-both{% else %}row-partial{% endif %}">
      <td class="name-col">{{ s.student_name }}</td>
      <td class="mark-col">{% if s.watched_video %}<span class="icon-yes">&#10003;</span>{% else %}<span class="icon-no">&#10007;</span>{% endif %}</td>
      <td class="mark-col">{% if s.shared_work %}<span class="icon-yes">&#10003;</span>{% else %}<span class="icon-no">&#10007;</span>{% endif %}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% endfor %}

<div class="footer">
  <span>TAP Student Report</span>
  <span>Generated on {{ generated_on }}</span>
</div>

</body></html>
"""


def _render_pdf(df, school_name, district_name, state_name, date_range_label, generated_on):
    from weasyprint import HTML
    from jinja2 import Template

    groups = _group_by_grade(df)
    html = Template(_PDF_TEMPLATE).render(
        school_name=school_name,
        district_name=district_name,
        state_name=state_name,
        date_range_label=date_range_label,
        groups=groups,
        generated_on=generated_on,
    )
    return HTML(string=html).write_pdf()


def _blob_path_for_school(school_id):
    return f"teacher_reporting/{school_id}.pdf"


def _upload_pdf_to_gcs(bucket, school_id, pdf_bytes):
    blob = bucket.blob(_blob_path_for_school(school_id))
    blob.upload_from_string(pdf_bytes, content_type="application/pdf")
    return blob


def _signed_url(blob, minutes):
    from datetime import timedelta as _td
    try:
        return blob.generate_signed_url(expiration=_td(minutes=minutes), method="GET")
    except Exception:
        blob.make_public()
        return blob.public_url


def _manifest_path(base):
    return base / MANIFEST_FILENAME


def _read_manifest(base):
    path = _manifest_path(base)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_manifest(base, manifest):
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f"{MANIFEST_FILENAME}.tmp"
    tmp.write_text(json.dumps(manifest, separators=(",", ":")))
    tmp.replace(_manifest_path(base))


def _run_in_progress():
    last = frappe.db.get_value(
        "Teacher Reporting Job", {}, ["status", "started_at"], order_by="creation desc", as_dict=True,
    )
    if not last or last.status != "RUNNING":
        return False
    elapsed = (now_datetime() - frappe.utils.get_datetime(last.started_at)).total_seconds() / 60
    return elapsed < LOCK_STALE_MINUTES


def _acquire_lock():
    if _run_in_progress():
        return None
    run = frappe.get_doc({
        "doctype": "Teacher Reporting Job", "status": "RUNNING", "started_at": now_datetime(),
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    return run


def _append_log_line(run, line):
    run.log = f"{run.log}\n{line}" if run.log else line
    run.save(ignore_permissions=True)
    frappe.db.commit()


def _persist_run(run, status, stats=None, error=None):
    run.status = status
    if status == "OK":
        s = stats or {}
        log_line = (
            f"{now_datetime()} — OK schools_uploaded={s.get('uploaded', 0)} "
            f"schools_skipped={s.get('skipped', 0)} rows={s.get('rows', 0)} "
            f"upload_failures={s.get('upload_failed', 0)} "
            f"duration_s={s.get('duration_s', 0)}"
        )
    else:
        run.error = error
        if sys.exc_info()[0]:
            run.traceback = frappe.get_traceback()
        log_line = f"{now_datetime()} — ERROR: {error}"
    _append_log_line(run, log_line)


def _render_and_upload_one_school(school_id, chunk, school_info, bucket,
                                   date_range_label, generated_on):
    school_name = school_info.get(school_id) if school_info else None
    if not school_name:
        return {
            "school_id": school_id, "ok": False,
            "reason": "school_not_found_in_lms", "rows": int(len(chunk)),
        }
    name, district_name, state_name = school_name
    name = _clean_school_display_name(name)
    if not name:
        return {
            "school_id": school_id, "ok": False,
            "reason": "school_not_found_in_lms", "rows": int(len(chunk)),
        }

    try:
        agg = _compute_school_aggregates(chunk)
        pdf_bytes = _render_pdf(chunk, name, district_name, state_name, date_range_label, generated_on)
        blob = _upload_pdf_to_gcs(bucket, school_id, pdf_bytes)
        return {
            "school_id": school_id, "ok": True,
            "blob_path": blob.name,
            "school_name": name,
            "district": district_name,
            "state": state_name,
            "total": agg["total"], "watched": agg["watched"], "shared": agg["shared"],
        }
    except Exception as exc:
        return {
            "school_id": school_id, "ok": False,
            "reason": "render_or_upload_failed", "rows": int(len(chunk)),
            "error_repr": f"{type(exc).__name__}: {exc}",
        }


def _iter_batches(items, batch_size):
    for i in range(0, len(items), batch_size):
        yield items[i:i + batch_size]


def run_weekly_extract():
    run_started = now_datetime()
    settings = get_reporting_settings()
    if not settings.enabled:
        return {"status": "disabled"}

    run = _acquire_lock()
    if not run:
        return {"status": "skipped", "message": "already running"}

    ok, missing_fields = _required_fields_ok(settings)
    if not ok:
        _persist_run(run, "ERROR", error=f"Missing settings: {', '.join(missing_fields)}")
        return {"status": "failure", "message": f"Missing settings: {', '.join(missing_fields)}"}

    base = _resolve_parquet_base(settings.parquet_dir)

    try:
        start, end = _last_week_window(now_datetime())
        date_range_label = _format_date_range_label(start, end)
        df = _run_bq_query(settings, start, end)
        df = _validate_and_normalize(df)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "teacher_reporting: query failed")
        _persist_run(run, "ERROR", error="BigQuery extract failed")
        return {"status": "failure"}

    try:
        distinct_ids = df["student_id"].dropna().unique().tolist()
        name_map = _resolve_student_names_bulk(distinct_ids)
        df = _attach_resolved_names(df, name_map)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "teacher_reporting: name resolution failed")
        _persist_run(run, "ERROR", error="Student name resolution failed")
        return {"status": "failure"}

    try:
        bucket = get_gcs_bucket(settings.gcs_settings_name)
        if bucket is None:
            raise ValueError(f"GCS Settings {settings.gcs_settings_name} is missing or not enabled")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "teacher_reporting: GCS setup failed")
        _persist_run(run, "ERROR", error="GCS Settings invalid or GCS client init failed")
        return {"status": "failure"}

    uploaded = {}
    skipped = {}
    schools_with_names = []

    total_rows = int(len(df))
    unresolved_total = int((df["student_name"] == "").sum())

    for school_id, chunk in df.groupby("school_id", sort=False):
        if not _looks_like_school_id(school_id):
            logged_id = school_id if len(school_id) <= _MAX_SCHOOL_ID_LEN_IN_LOG else (
                school_id[:_MAX_SCHOOL_ID_LEN_IN_LOG] + "...<truncated>"
            )
            skipped[logged_id] = "school_id_malformed"
            continue
        has_any_name = bool((chunk["student_name"] != "").any())
        if not has_any_name:
            skipped[school_id] = "no_resolved_names"
            continue
        schools_with_names.append((school_id, chunk))

    try:
        school_info = _resolve_school_names_bulk(sid for sid, _ in schools_with_names)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "teacher_reporting: school name resolution failed")
        _persist_run(run, "ERROR", error="School name resolution failed")
        return {"status": "failure"}

    generated_on = now_datetime().strftime("%d %b %Y")

    _append_log_line(
        run,
        f"{now_datetime()} — extract done: rows={total_rows} schools_total="
        f"{len(schools_with_names) + len(skipped)} eligible={len(schools_with_names)} "
        f"skipped_upfront={len(skipped)} unresolved_name_rows={unresolved_total} "
        f"schools_resolved_in_lms={len(school_info)}"
    )

    max_workers = settings.max_workers
    batch_size = settings.batch_size
    upload_failed = 0
    batches = list(_iter_batches(schools_with_names, batch_size))
    total_batches = len(batches)

    for batch_idx, batch in enumerate(batches, start=1):
        batch_error_samples = []
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(
                        _render_and_upload_one_school, school_id, chunk, school_info,
                        bucket, date_range_label, generated_on,
                    ): school_id
                    for school_id, chunk in batch
                }
                for fut in as_completed(futures):
                    sid = futures[fut]
                    try:
                        result = fut.result()
                    except Exception as exc:
                        skipped[sid] = "worker_exception"
                        upload_failed += 1
                        batch_error_samples.append(f"{sid}: {type(exc).__name__}: {exc}")
                        continue

                    if result["ok"]:
                        uploaded[result["school_id"]] = {
                            "blob_path": result["blob_path"],
                            "school_name": result["school_name"],
                            "district": result["district"],
                            "state": result["state"],
                            "total": result["total"],
                            "watched": result["watched"],
                            "shared": result["shared"],
                        }
                    else:
                        skipped[result["school_id"]] = result["reason"]
                        if result["reason"] == "render_or_upload_failed":
                            upload_failed += 1
                            if result.get("error_repr") and len(batch_error_samples) < 5:
                                batch_error_samples.append(f"{result['school_id']}: {result['error_repr']}")
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"teacher_reporting: batch {batch_idx} failed")
            for school_id, _chunk in batch:
                skipped.setdefault(school_id, "batch_failed")

        if batch_error_samples:
            frappe.log_error(
                "\n".join(batch_error_samples),
                f"teacher_reporting: {len(batch_error_samples)} render/upload failure sample(s) "
                f"in batch {batch_idx}/{total_batches}",
            )

        _append_log_line(
            run,
            f"{now_datetime()} — batch {batch_idx}/{total_batches} done: "
            f"uploaded_so_far={len(uploaded)} skipped_so_far={len(skipped)} "
            f"upload_failures_so_far={upload_failed}"
        )

    manifest = {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "date_range_label": date_range_label,
        "generated_at": now_datetime().isoformat(),
        "schools": uploaded,
        "skipped": skipped,
    }
    try:
        _write_manifest(base, manifest)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "teacher_reporting: manifest write failed")
        _persist_run(run, "ERROR", error="Manifest write failed (PDFs were still uploaded to GCS)")
        return {
            "status": "failure",
            "message": "Manifest write failed; PDFs are in GCS but not yet indexed locally.",
            "uploaded": len(uploaded), "skipped": len(skipped),
        }

    duration_s = round((now_datetime() - run_started).total_seconds(), 1)
    stats = {
        "uploaded": len(uploaded),
        "skipped": len(skipped),
        "rows": total_rows,
        "unresolved_name_rows": unresolved_total,
        "upload_failed": upload_failed,
        "duration_s": duration_s,
    }
    _persist_run(run, "OK", stats=stats)
    return {"status": "success", **stats}


def _manifest_age_days(manifest):
    generated_at = manifest.get("generated_at")
    if not generated_at:
        return None
    try:
        generated_dt = frappe.utils.get_datetime(generated_at)
    except Exception:
        return None
    return (now_datetime() - generated_dt).total_seconds() / 86400


@frappe.whitelist(allow_guest=True)
def teacher_report(school_id, api_key):
    try:
        settings = get_reporting_settings()
        if not authenticate_api_key(api_key):
            frappe.local.response.update({"status": "failure", "message": "Invalid or missing API key"})
            return

        school_id = _normalize_school_id(school_id)
        if not school_id or not _looks_like_school_id(school_id):
            frappe.local.response.update({"status": "failure", "message": "school_id is required/invalid"})
            return

        base = _resolve_parquet_base(settings.parquet_dir)
        manifest = _read_manifest(base)
        if not manifest:
            frappe.local.response.update({
                "status": "failure",
                "message": "No report data available yet. The weekly extract may not have run.",
            })
            return

        age_days = _manifest_age_days(manifest)
        if age_days is not None and age_days > settings.stats_ttl_days:
            frappe.local.response.update({
                "status": "failure",
                "message": (
                    f"Report data is stale (last generated {age_days:.1f} day(s) ago, "
                    f"older than the {settings.stats_ttl_days}-day limit). The weekly "
                    f"extract may have failed to run recently."
                ),
            })
            return

        entry = (manifest.get("schools") or {}).get(school_id)
        if not entry:
            reason = (manifest.get("skipped") or {}).get(school_id)
            if reason == "no_resolved_names":
                message = (
                    f"No report available for school_id {school_id} in week "
                    f"{manifest.get('date_range_label', '?')} -- no student names could be "
                    f"resolved for this school this week."
                )
            elif reason == "school_not_found_in_lms":
                message = f"School {school_id} not found in LMS."
            elif reason:
                message = f"No report available for school_id {school_id} ({reason})."
            else:
                message = (
                    f"No data for school_id {school_id} in week "
                    f"{manifest.get('date_range_label', '?')}"
                )
            frappe.local.response.update({"status": "failure", "message": message})
            return

        if not settings.gcs_settings_name:
            frappe.local.response.update({"status": "failure", "message": "GCS Settings not configured"})
            return

        try:
            bucket = get_gcs_bucket(settings.gcs_settings_name)
            if bucket is None:
                raise ValueError(f"GCS Settings {settings.gcs_settings_name} is missing or not enabled")
            blob = bucket.blob(entry["blob_path"])
            if not blob.exists(bucket.client):
                frappe.local.response.update({
                    "status": "failure",
                    "message": f"Report file for {school_id} is missing in storage. Please retry after the next run.",
                })
                return
            url = _signed_url(blob, minutes=settings.signed_url_ttl_minutes)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "teacher_report: signing failed")
            frappe.local.response.update({"status": "failure", "message": "Failed to generate a download link"})
            return

        sheet_url = f"{url}#file.pdf"

        return {
            "status": "ok",
            "school_id": school_id,
            "school_name": entry.get("school_name", ""),
            "state": entry.get("state", ""),
            "district": entry.get("district", ""),
            "date_range_label": manifest.get("date_range_label", ""),
            "total_students": entry.get("total", 0),
            "school_watched": entry.get("watched", 0),
            "school_not_watched": entry.get("total", 0) - entry.get("watched", 0),
            "school_shared": entry.get("shared", 0),
            "sheet": sheet_url,
        }
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "teacher_report failed")
        frappe.local.response.update({"status": "failure", "message": f"{type(exc).__name__}: {exc}"})
        return


def get_teacher_reporting_stats():
    settings = get_reporting_settings()
    result = {
        "enabled": bool(settings.enabled),
        "configured": False,
        "manifest_found": False,
        "stale": None,
        "age_days": None,
        "stats_ttl_days": settings.stats_ttl_days,
        "signed_url_ttl_minutes": settings.signed_url_ttl_minutes,
        "week_start": None,
        "week_end": None,
        "date_range_label": None,
        "generated_at": None,
        "schools_uploaded": 0,
        "schools_skipped": 0,
        "skipped_reasons": {},
        "last_run_at": None,
        "last_run_status": None,
        "message": None,
    }

    last = frappe.db.get_value(
        "Teacher Reporting Job", {}, ["started_at", "status"], order_by="creation desc", as_dict=True,
    )
    if last:
        result["last_run_at"] = last.started_at
        result["last_run_status"] = last.status

    result["configured"] = True

    try:
        base = _resolve_parquet_base(settings.parquet_dir)
    except ValueError as exc:
        result["message"] = str(exc)
        return result

    manifest = _read_manifest(base)
    if not manifest:
        result["message"] = "No manifest found yet. The weekly extract may not have run."
        return result

    result["manifest_found"] = True
    result["week_start"] = manifest.get("week_start")
    result["week_end"] = manifest.get("week_end")
    result["date_range_label"] = manifest.get("date_range_label")
    result["generated_at"] = manifest.get("generated_at")

    schools = manifest.get("schools") or {}
    skipped = manifest.get("skipped") or {}
    result["schools_uploaded"] = len(schools)
    result["schools_skipped"] = len(skipped)

    reason_counts = {}
    for reason in skipped.values():
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    result["skipped_reasons"] = reason_counts

    age_days = _manifest_age_days(manifest)
    result["age_days"] = round(age_days, 2) if age_days is not None else None
    result["stale"] = bool(age_days is not None and age_days > settings.stats_ttl_days)

    if result["stale"]:
        result["message"] = (
            f"Report data is stale: last generated {result['age_days']} day(s) ago, "
            f"older than the {settings.stats_ttl_days}-day limit."
        )

    return result


def _safe_rename_and_remove(path: Path):
    if not path.exists():
        return False
    trash_name = path.with_name(f".{path.name}.trash-{uuid.uuid4().hex[:8]}")
    path.rename(trash_name)
    shutil.rmtree(trash_name, ignore_errors=True)
    return True


def _safe_remove_file(path: Path):
    if not path.exists():
        return False
    trash_name = path.with_name(f".{path.name}.trash-{uuid.uuid4().hex[:8]}")
    path.rename(trash_name)
    try:
        trash_name.unlink()
    except Exception:
        pass
    return True


def _resolve_parquet_base(parquet_dir_value):
    raw = (parquet_dir_value or "").strip()
    if not raw:
        raise ValueError("Parquet Storage Dir is not set.")
    base = Path(raw).expanduser().resolve()
    if base == base.anchor or str(base) in ("/", ""):
        raise ValueError(f"Refusing to clear an unsafe path: {base}")
    return base


def clear_parquet_cache():
    if _run_in_progress():
        raise ValueError(
            "A weekly extract is currently running. Try again once it finishes "
            "(or wait a few minutes if it looks stuck)."
        )

    parquet_dir_value = get_reporting_settings().parquet_dir
    base = _resolve_parquet_base(parquet_dir_value)

    if not base.exists():
        return {"status": "noop", "message": f"{base} does not exist, nothing to clear."}

    cleared = []

    if _safe_remove_file(_manifest_path(base)):
        cleared.append(str(_manifest_path(base)))

    legacy_live = base / _LEGACY_LIVE_DIRNAME
    if _safe_rename_and_remove(legacy_live):
        cleared.append(str(legacy_live))

    legacy_staging = base / _LEGACY_STAGING_DIRNAME
    if _safe_rename_and_remove(legacy_staging):
        cleared.append(str(legacy_staging))

    legacy_previous = base / _LEGACY_LIVE_PARENT / _LEGACY_PREVIOUS_DIRNAME
    if _safe_rename_and_remove(legacy_previous):
        cleared.append(str(legacy_previous))

    legacy_missing = base / _LEGACY_MISSING_FILENAME
    if _safe_remove_file(legacy_missing):
        cleared.append(str(legacy_missing))

    for tmp_leftover in base.glob("**/*.tmp"):
        try:
            tmp_leftover.unlink()
            cleared.append(str(tmp_leftover))
        except Exception:
            pass

    log_line = f"{now_datetime()} — CACHE CLEARED by {frappe.session.user} ({len(cleared)} paths)"
    existing = get_reporting_settings().get("last_run_log") or ""
    lines = (existing.splitlines() + [log_line])[-50:]
    frappe.db.set_value("Teacher Reporting Settings", "Teacher Reporting Settings", "last_run_log", "\n".join(lines))
    frappe.db.commit()

    return {"status": "success", "cleared": cleared, "count": len(cleared)}
