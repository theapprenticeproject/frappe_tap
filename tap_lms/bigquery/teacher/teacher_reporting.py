"""
Teacher Reporting.

Weekly: pulls per-student engagement rows from BigQuery (no reliable name
column there -- grade still comes from BigQuery, but names are resolved in
bulk against the Student doctype by student_id), writes one Parquet file
per school_id to a fixed, permanent path via an atomic staging-dir swap.
Triggered every Monday at 3 pm IST by hooks.py (run_weekly_extract).

On-demand: Glific calls teacher_report(school_id, api_key); reads that
school's parquet straight off disk, renders a PDF, uploads it to GCS
(credentials/bucket from GCS Settings) at a fixed per-school path with
Content-Type: application/pdf, and returns a signed URL.

Maintenance: clear_parquet_cache() lets an admin wipe the on-disk parquet
cache (live + staging + missing.jsonl) from the BigQuery Settings doctype,
safely -- it never deletes in place, only ever renames-then-removes, so a
crash mid-clear can't leave the live/ directory half-deleted.

Only existing doctypes are used: BigQuery Settings, GCS Settings, Student,
School, District.
"""

import hmac
import json
import re
import shutil
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

import frappe
from frappe.utils import cint, now_datetime

SETTINGS_DOCTYPE = "BigQuery Settings"

LOCK_STALE_MINUTES = 10
STAGING_DIRNAME = ".staging/teacher_reporting"
LIVE_DIRNAME = "live/teacher_reporting"
STUDENT_CHUNK_SIZE = 5000
GRADE_ORDER_FALLBACK = 99

REQUIRED_SETTING_FIELDS = (
    "project_id", "credentials_json", "dataset_id", "contacts_table",
    "messages_table", "main_flow_name", "group_label_pattern",
    "parquet_dir", "gcs_settings_name", "secret",
)

# --------------------------------------------------------------------------
# school_id validation
#
# Only two known-good shapes exist in real data:
#   1. "SC" + exactly 8 digits           e.g. SC00005086
#   2. 2 letters + 2 letters + 2 alnum   e.g. GHBA23LT, RABA71OX
#
# Matched as a WHOLE string (fullmatch) -- no leading/trailing junk
# tolerated. Anything else (WhatsApp message text, mixed-in names,
# stray characters, wrong length) is rejected outright rather than
# "looking plausible".
# --------------------------------------------------------------------------

_SCHOOL_ID_PATTERNS = (
    re.compile(r"^SC\d{8}$"),
    re.compile(r"^[A-Z]{2}[A-Z]{2}[A-Z0-9]{2}$"),
)
_MAX_SCHOOL_ID_LEN_IN_LOG = 80  # truncate junk before writing to missing.jsonl

# --------------------------------------------------------------------------
# student_id validation (gates the bulk LMS name lookup)
#
# BigQuery's student_id values ultimately originate from end users and can
# contain anything -- stray whitespace, embedded newlines, free text, etc.
# Before using them as a SQL "in" filter against the Student doctype we
# only keep values that look like real LMS student ids, i.e. the same
# "ST" + 8 digits shape autoname() generates. Anything else is dropped
# from the lookup (it will simply fail to resolve to a name, same as a
# genuinely unmatched id) rather than passed through to the database.
# --------------------------------------------------------------------------

_STUDENT_ID_PATTERN = re.compile(r"^ST\d{8}$")


def _normalize_school_id(value):
    """Uppercase + strip. Does NOT validate shape."""
    return (value or "").strip().upper()


def _looks_like_school_id(value):
    """value must already be normalized (see _normalize_school_id)."""
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


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def get_reporting_settings():
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    return frappe._dict({
        "enabled": cint(settings.get("tsp_enabled")),
        "project_id": (settings.get("project_id") or "").strip(),
        "credentials_json": settings.get_password("credentials_json", raise_exception=False),
        "dataset_id": (settings.get("tsp_dataset_id") or "").strip(),
        "contacts_table": (settings.get("tsp_contacts_table") or "").strip(),
        "messages_table": (settings.get("tsp_messages_table") or "").strip(),
        "group_label_pattern": (settings.get("tsp_group_label_pattern") or "").strip(),
        "main_flow_name": (settings.get("tsp_main_flow_name") or "").strip(),
        "watched_flow_label": (settings.get("tsp_watched_flow_label") or "%Activity_Access%").strip(),
        "shared_flow_label": (settings.get("tsp_shared_flow_label") or "%Activity_Submission%").strip(),
        "parquet_dir": (settings.get("tsp_parquet_dir") or "").strip(),
        "gcs_settings_name": settings.get("tsp_gcs_settings"),
        "secret": settings.get_password("tsp_secret", raise_exception=False),
        "max_query_gb": cint(settings.get("tsp_max_query_gb")) or 10,
    })


def _required_fields_ok(settings):
    missing = [f for f in REQUIRED_SETTING_FIELDS if not settings.get(f)]
    return (not missing), missing


def _get_gcs_settings(gcs_settings_name):
    doc = frappe.get_doc("GCS Settings", gcs_settings_name)
    if not cint(doc.enabled):
        raise ValueError(f"GCS Settings {gcs_settings_name} is not enabled")
    return frappe._dict({
        "bucket_name": (doc.bucket_name or "").strip(),
        "project_id": (doc.project_id or "").strip(),
        "credentials_json": doc.get_password("credentials_json", raise_exception=False),
        "bucket_type": doc.bucket_type,
    })


# --------------------------------------------------------------------------
# BigQuery
# --------------------------------------------------------------------------

def get_bq_client(settings):
    from google.cloud import bigquery
    from google.oauth2 import service_account

    if not settings.credentials_json:
        raise ValueError("BigQuery Settings: Credentials JSON is not set")
    if not settings.project_id:
        raise ValueError("BigQuery Settings: Project ID is not set")

    info = json.loads(settings.credentials_json)
    credentials = service_account.Credentials.from_service_account_info(info)
    return bigquery.Client(project=settings.project_id, credentials=credentials)


# NOTE: grade is sourced from BigQuery's raw_fields (it is reliable there).
# Student *names* are deliberately NOT selected here -- BQ's own name data
# is not trustworthy, so names are resolved afterwards in bulk against the
# Student doctype, keyed by student_id (see _resolve_student_names_bulk).
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


def _fully_qualified(settings, table_name):
    return f"{settings.project_id}.{settings.dataset_id}.{table_name}"


def _last_week_window(ref_dt):
    end = ref_dt.date() - timedelta(days=1)
    start = end - timedelta(days=6)
    return start, end


def _run_bq_query(settings, start, end, client=None):
    from google.cloud import bigquery
    import pandas as pd

    client = client or get_bq_client(settings)
    query = QUERY_TEMPLATE.format(
        contacts_table_fq=_fully_qualified(settings, settings.contacts_table),
        messages_table_fq=_fully_qualified(settings, settings.messages_table),
    )
    params = [
        bigquery.ScalarQueryParameter("group_label_pattern", "STRING", settings.group_label_pattern),
        bigquery.ScalarQueryParameter("main_flow_name", "STRING", settings.main_flow_name),
        bigquery.ScalarQueryParameter("watched_flow_label", "STRING", settings.watched_flow_label),
        bigquery.ScalarQueryParameter("shared_flow_label", "STRING", settings.shared_flow_label),
        bigquery.ScalarQueryParameter("window_start", "DATE", start),
        bigquery.ScalarQueryParameter("window_end", "DATE", end),
    ]
    job_config = bigquery.QueryJobConfig(
        query_parameters=params,
        maximum_bytes_billed=int(settings.max_query_gb) * (1024 ** 3),
    )
    rows = client.query(query, job_config=job_config).result()
    df = rows.to_dataframe(create_bqstorage_client=False)
    if df.empty:
        df = pd.DataFrame(columns=["student_id", "grade", "school_id", "watched_video", "shared_work"])
    df["student_id"] = df["student_id"].astype(str)
    df["school_id"] = df["school_id"].astype(str)
    df["grade"] = df["grade"].astype(str)
    df["watched_video"] = df["watched_video"].fillna(0).astype(int)
    df["shared_work"] = df["shared_work"].fillna(0).astype(int)
    return df


# --------------------------------------------------------------------------
# bulk LMS name resolution (by student_id)
# --------------------------------------------------------------------------

_NAME_WHITESPACE_RE = re.compile(r"\s+")
_NAME_LETTER_RUN_RE = re.compile(r"[A-Za-z]+")
_MAX_NAME_LEN = 120


def _titlecase_name(name):
    """Normalize an LMS name value into clean 'First Last' title case.

    Safe on any input -- never raises. Handles non-string values (coerces
    to str), None/blank, excess/irregular whitespace (double spaces,
    tabs, newlines -- all collapsed to single spaces so multi-word names
    come out clean), absurdly long junk strings (hard-capped), and mixed
    casing. Capitalizes the first letter of every run of letters, so
    names with hyphens or apostrophes are capitalized correctly on both
    sides (e.g. "anne-marie o'brien" -> "Anne-Marie O'Brien").
    """
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
    """student_ids -> {student_id: title-cased name}. Missing/blank names are absent.

    Safety: every id is normalized (stripped) and must match the LMS
    student_id shape (ST + 8 digits) before it is allowed into the SQL
    "in" filter -- BigQuery-sourced ids are user-influenced data and are
    not trusted as-is. Ids are also de-duplicated before chunking so a
    noisy BQ extract can't inflate the number/size of lookup queries.

    Optimization: frappe.get_all's "in" filter is parameterized by the
    framework (safe against injection), de-duping + chunking keeps each
    query bounded in size, and only the two needed columns are fetched.
    """
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
        rows = frappe.get_all(
            "Student",
            filters={"profile_id": ["in", chunk]},
            fields=["profile_id", "name1"],
        )
        for row in rows:
            if not row.profile_id:
                continue
            clean_name = _titlecase_name(row.name1)
            if clean_name:
                resolved[row.profile_id] = clean_name
    return resolved


def _attach_resolved_names(df, name_map):
    df["student_name"] = df["student_id"].map(name_map).fillna("")
    return df


# --------------------------------------------------------------------------
# weekly extract: staging write + atomic swap
# --------------------------------------------------------------------------

def _validate_chunk(df):
    df = df[(df["school_id"].str.strip() != "") & (df["student_id"].str.strip() != "")].copy()
    # Normalize school_id up front (uppercase + strip) so "sc00000678" and
    # "SC00000678" are the same school, and downstream grouping/validation
    # all see the normalized value.
    df["school_id"] = df["school_id"].map(_normalize_school_id)
    return df


def _write_parquet_per_school(df, staging_dir):
    import pyarrow as pa
    import pyarrow.parquet as pq

    written = {}
    failures = []
    cols = ["student_id", "student_name", "grade", "watched_video", "shared_work"]

    for school_id, chunk in df.groupby("school_id", sort=False):
        if not _looks_like_school_id(school_id):
            logged_id = school_id if len(school_id) <= _MAX_SCHOOL_ID_LEN_IN_LOG else (
                school_id[:_MAX_SCHOOL_ID_LEN_IN_LOG] + "...<truncated>"
            )
            failures.append({"school_id": logged_id, "reason": "school_id_malformed", "rows": int(len(chunk))})
            continue
        try:
            table = pa.Table.from_pandas(chunk[cols].reset_index(drop=True), preserve_index=False)
            pq.write_table(table, staging_dir / f"{school_id}.parquet")
            written[school_id] = {"rows": int(len(chunk))}
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"teacher_reporting: write failed for {school_id}")
            failures.append({"school_id": school_id, "reason": "parquet_write_failed", "rows": int(len(chunk))})

    return written, failures


def _write_missing(parquet_dir, write_failures, unresolved_total):
    path = Path(parquet_dir) / "missing.jsonl"
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w") as fh:
        for entry in write_failures:
            fh.write(json.dumps({
                "school_id": entry["school_id"],
                "reason": entry["reason"],
                "rows": entry.get("rows"),
            }) + "\n")
        if unresolved_total:
            fh.write(json.dumps({
                "school_id": None,
                "reason": f"{unresolved_total} student rows had no resolvable LMS name this week",
            }) + "\n")
    tmp.replace(path)


def _format_date_range_label(start, end):
    # e.g. "5 Nov - 11 Nov 2026". Day has no leading zero; month is short
    # form; the year is shown once, at the end, even when the range spans
    # two different months.
    start_label = f"{start.day} {start.strftime('%b')}"
    end_label = f"{end.day} {end.strftime('%b')} {end.year}"
    return f"{start_label} - {end_label}"


def _write_manifest_into(staging_dir, written, start, end):
    manifest = {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "date_range_label": _format_date_range_label(start, end),
        "generated_at": now_datetime().isoformat(),
        "schools": written,
    }
    tmp = staging_dir / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, separators=(",", ":")))
    tmp.replace(staging_dir / "manifest.json")


def _swap_staging_live(base, staging, live):
    live.parent.mkdir(parents=True, exist_ok=True)
    previous = base / "live" / "teacher_reporting.previous"
    if previous.exists():
        shutil.rmtree(previous, ignore_errors=True)
    if live.exists():
        live.rename(previous)
    staging.rename(live)
    if previous.exists():
        shutil.rmtree(previous, ignore_errors=True)


def _acquire_lock():
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    status = settings.get("tsp_last_run_status")
    started_at = settings.get("tsp_last_run_at")
    if status == "RUNNING" and started_at:
        elapsed = (now_datetime() - frappe.utils.get_datetime(started_at)).total_seconds() / 60
        if elapsed < LOCK_STALE_MINUTES:
            return False
    settings.tsp_last_run_status = "RUNNING"
    settings.tsp_last_run_at = now_datetime()
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    return True


def _persist_run(status, schools=0, missing=0, rows=0, error=None):
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    settings.tsp_last_run_at = now_datetime()
    settings.tsp_last_run_status = status
    if status == "OK":
        log_line = f"{now_datetime()} — OK schools={schools} rows={rows} missing={missing}"
    else:
        log_line = f"{now_datetime()} — ERROR: {error}"
    existing = settings.get("tsp_last_run_log") or ""
    lines = (existing.splitlines() + [log_line])[-50:]
    settings.tsp_last_run_log = "\n".join(lines)
    settings.save(ignore_permissions=True)
    frappe.db.commit()


def run_weekly_extract():
    """Entry point for the Monday 3pm IST scheduler hook (see hooks.py).

    No-ops immediately unless "Enable Teacher Reporting" is checked in
    BigQuery Settings, so wiring this into cron is inert by default.
    """
    settings = get_reporting_settings()
    if not settings.enabled:
        return {"status": "disabled"}

    ok, missing_fields = _required_fields_ok(settings)
    if not ok:
        _persist_run("ERROR", error=f"Missing settings: {', '.join(missing_fields)}")
        return {"status": "failure", "message": f"Missing settings: {', '.join(missing_fields)}"}

    if not _acquire_lock():
        return {"status": "skipped", "message": "already running"}

    base = Path(settings.parquet_dir)
    staging = base / STAGING_DIRNAME
    live = base / LIVE_DIRNAME

    try:
        start, end = _last_week_window(now_datetime())
        df = _run_bq_query(settings, start, end)
        df = _validate_chunk(df)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "teacher_reporting: query failed")
        _persist_run("ERROR", error="BigQuery extract failed")
        return {"status": "failure"}

    try:
        distinct_ids = df["student_id"].dropna().unique().tolist()
        name_map = _resolve_student_names_bulk(distinct_ids)
        df = _attach_resolved_names(df, name_map)

        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        written, failures = _write_parquet_per_school(df, staging)
        unresolved_count = int((df["student_name"] == "").sum())
        _write_missing(base, failures, unresolved_count)
        _write_manifest_into(staging, written, start, end)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "teacher_reporting: staging write failed")
        _persist_run("ERROR", error="Staging write failed")
        return {"status": "failure"}

    try:
        _swap_staging_live(base, staging, live)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "teacher_reporting: swap failed")
        _persist_run("ERROR", error="Staging-to-live swap failed")
        return {"status": "failure"}

    _persist_run("OK", schools=len(written), missing=len(failures), rows=len(df))
    return {"status": "success", "schools": len(written), "missing": len(failures), "rows": int(len(df))}


# --------------------------------------------------------------------------
# on-demand: school/district/state resolution (LMS)
# --------------------------------------------------------------------------

def _resolve_school_names(school_id):
    school = frappe.db.get_value(
        "School", {"keyword": school_id}, ["name1", "district", "state"], as_dict=True
    )
    if not school:
        return "", "", ""
    school_name = (school.name1 or "").strip()
    state_name = (school.state or "").strip()
    district_name = ""
    if school.district:
        district_name = school.district
        if not state_name:
            state_name = (frappe.db.get_value("District", school.district, "state") or "").strip()
    return school_name, district_name, state_name


def _clean_school_display_name(name):
    return re.sub(r"\s+", " ", (name or "")).strip()


# --------------------------------------------------------------------------
# on-demand: read parquet + aggregates
# --------------------------------------------------------------------------

def _read_manifest(live_dir):
    path = live_dir / "manifest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _compute_school_aggregates(df):
    total = int(len(df))
    watched = int(df["watched_video"].sum()) if total else 0
    shared = int(df["shared_work"].sum()) if total else 0
    return {"watched": watched, "not_watched": total - watched, "shared": shared, "total": total}


def _grade_sort_key(grade_label):
    digits = re.match(r"^\d+", grade_label or "")
    return (0, int(digits.group())) if digits else (1, grade_label or "")


def _status_rank(watched, shared):
    """Mirrors reportGen_statusRank_ in the Apps Script: both ticked first,
    then one ticked, then neither."""
    watched = bool(watched)
    shared = bool(shared)
    if watched and shared:
        return 0
    if watched or shared:
        return 1
    return 2


def _group_by_grade(df):
    """Builds per-grade student groups, sorted by grade then by status/name.

    Vectorized: status rank and the final sort are computed with pandas
    ops across the whole frame once, rather than per student_row in a
    Python loop, then split into groups -- this keeps the on-demand PDF
    render fast even for large schools.
    """
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


# --------------------------------------------------------------------------
# on-demand: PDF render + GCS upload
# --------------------------------------------------------------------------
#
# Square corners throughout (no border-radius), compact header, grade
# tables flow continuously across page breaks (no forced break before a
# new grade), and each row is fully tinted yellow (partial) or green
# (both) with no separate icon chip background.

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

/* ---- header: compact, square corners ---- */
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

/* ---- grade blocks: flow continuously, no forced page break ---- */
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
thead { display: table-header-group; } /* repeat column header on each page */
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

.icon-yes, .icon-no {
  font-weight: 700;
  font-size: 13px;
}
.icon-yes { color: #145C3A; }
.icon-no { color: #A33A34; }

/* full-row tint, no separate chip behind the tick/cross */
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


def _render_pdf(df, school_name, district_name, state_name, date_range_label):
    from weasyprint import HTML
    from jinja2 import Template

    groups = _group_by_grade(df)
    html = Template(_PDF_TEMPLATE).render(
        school_name=school_name,
        district_name=district_name,
        state_name=state_name,
        date_range_label=date_range_label,
        groups=groups,
        generated_on=now_datetime().strftime("%d %b %Y"),
    )
    return HTML(string=html).write_pdf()


def _gcs_client(gcs_settings):
    from google.cloud import storage
    from google.oauth2 import service_account

    info = json.loads(gcs_settings.credentials_json)
    credentials = service_account.Credentials.from_service_account_info(info)
    return storage.Client(project=gcs_settings.project_id, credentials=credentials)


def _upload_pdf_to_gcs(gcs_settings, school_id, pdf_bytes):
    client = _gcs_client(gcs_settings)
    bucket = client.bucket(gcs_settings.bucket_name)
    blob = bucket.blob(f"teacher_reporting/{school_id}.pdf")
    blob.upload_from_string(pdf_bytes, content_type="application/pdf")
    return blob


def _signed_url(blob, minutes=60):
    from datetime import timedelta as _td
    try:
        return blob.generate_signed_url(expiration=_td(minutes=minutes), method="GET")
    except Exception:
        blob.make_public()
        return blob.public_url


def _constant_time_eq(a, b):
    return hmac.compare_digest(str(a or ""), str(b or ""))


# --------------------------------------------------------------------------
# on-demand endpoint
# --------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def teacher_report(school_id, api_key):
    try:
        import pyarrow.parquet as pq

        settings = get_reporting_settings()
        if not settings.secret or not _constant_time_eq(api_key, settings.secret):
            frappe.local.response.update({"status": "failure", "message": "Invalid or missing API key"})
            return

        school_id = _normalize_school_id(school_id)
        if not school_id or not _looks_like_school_id(school_id):
            frappe.local.response.update({"status": "failure", "message": "school_id is required/invalid"})
            return

        if not settings.parquet_dir:
            frappe.local.response.update({"status": "failure", "message": "Reporting is not configured"})
            return

        live_dir = Path(settings.parquet_dir) / LIVE_DIRNAME
        manifest = _read_manifest(live_dir)
        entry = (manifest.get("schools") or {}).get(school_id)
        if not entry:
            frappe.local.response.update({
                "status": "failure",
                "message": f"No data for school_id {school_id} in week {manifest.get('date_range_label', '?')}",
            })
            return

        parquet_path = live_dir / f"{school_id}.parquet"
        if not parquet_path.exists():
            frappe.local.response.update({"status": "failure", "message": f"No report file for {school_id}"})
            return

        df = pq.read_table(parquet_path).to_pandas()

        school_name, district_name, state_name = _resolve_school_names(school_id)
        if not school_name:
            frappe.local.response.update({"status": "failure", "message": f"School {school_id} not found in LMS"})
            return
        school_name = _clean_school_display_name(school_name)

        agg = _compute_school_aggregates(df)
        pdf_bytes = _render_pdf(df, school_name, district_name, state_name, manifest.get("date_range_label", ""))

        if not settings.gcs_settings_name:
            frappe.local.response.update({"status": "failure", "message": "GCS Settings not configured"})
            return
        gcs_settings = _get_gcs_settings(settings.gcs_settings_name)
        blob = _upload_pdf_to_gcs(gcs_settings, school_id, pdf_bytes)
        url = _signed_url(blob)
        # Some downstream platforms (e.g. certain WhatsApp/Glific senders)
        # only recognize a URL as "a PDF to send" if it carries a markdown
        # link whose visible text ends in a #file.pdf-style anchor. We
        # append that suffix to the SAME per-request signed url generated
        # above -- never a hardcoded example -- so the link stays valid
        # and still points at this exact signed URL.
        sheet_url = f"{url} [#file.pdf]({url}#file.pdf)"

        return {
            "status": "ok",
            "school_id": school_id,
            "school_name": school_name,
            "state": state_name,
            "district": district_name,
            "date_range_label": manifest.get("date_range_label", ""),
            "school_watched": agg["watched"],
            "school_not_watched": agg["not_watched"],
            "school_shared": agg["shared"],
            "sheet": sheet_url,
        }
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "teacher_report failed")
        frappe.local.response.update({"status": "failure", "message": f"{type(exc).__name__}: {exc}"})
        return


# --------------------------------------------------------------------------
# maintenance: clear the on-disk parquet cache safely
# --------------------------------------------------------------------------
#
# Called from the "Clear Parquet Cache" button on BigQuery Settings
# (see bigquery_settings.py::clear_parquet_cache). Design goals:
#
#   * Never delete anything in place. Every directory that is going away
#     is first renamed (an atomic, near-instant filesystem op) to a
#     throwaway name under the same parent, and only the renamed copy is
#     rmtree'd. If the process dies mid-delete, the live/staging dirs
#     under their real names are simply gone (rename already happened)
#     or simply still there (rename never happened) -- there is no
#     "half-deleted directory under the name teacher_reporting still
#     expects" state for the on-demand endpoint to trip over.
#   * Refuses to run while a weekly extract is RUNNING (same staleness
#     window as _acquire_lock), so it can't race the swap and leave live/
#     pointing at a directory that is being removed underneath it.
#   * Bounded to parquet_dir itself (resolved, existence-checked) so a
#     blank/garbage path can never resolve to something like "/" and
#     nothing outside tsp_parquet_dir is ever touched.
#   * Removes the stray ".previous" swap-rollback directory too, and
#     missing.jsonl, so nothing is left behind that could confuse a
#     later run or a later read.


def _safe_rename_and_remove(path: Path):
    """Rename `path` out of the way, then rmtree the renamed copy.

    No-op (returns False) if `path` does not exist. The rename makes the
    removal atomic from the point of view of anything else looking at
    `path`'s original name -- it either already moved (and this function
    is now cleaning up a tombstone no one else references) or it hasn't
    moved yet, never half-moved.
    """
    if not path.exists():
        return False
    trash_name = path.with_name(f".{path.name}.trash-{uuid.uuid4().hex[:8]}")
    path.rename(trash_name)
    shutil.rmtree(trash_name, ignore_errors=True)
    return True


def _resolve_parquet_base(parquet_dir_value):
    """Resolve + sanity-check tsp_parquet_dir before anything is touched.

    Returns a resolved Path, or raises ValueError with a message safe to
    show the user. Refuses empty/whitespace values and anything that
    resolves to the filesystem root, so a misconfigured or blank setting
    can never cause a wipe outside the intended cache tree.
    """
    raw = (parquet_dir_value or "").strip()
    if not raw:
        raise ValueError("Parquet Storage Dir is not set.")
    base = Path(raw).expanduser().resolve()
    if base == base.anchor or str(base) in ("/", ""):
        raise ValueError(f"Refusing to clear an unsafe path: {base}")
    return base


def clear_parquet_cache():
    """Safely wipe the teacher-reporting parquet cache (live + staging +
    the swap-rollback dir + missing.jsonl) under tsp_parquet_dir.

    Returns a dict summary; never raises for "nothing to clear" or "not
    configured" -- those are reported as part of the result, not errors.
    Raises ValueError only for a genuinely unsafe/misconfigured path or
    a currently-running weekly extract, both of which the caller should
    surface to the user as-is.
    """
    settings = frappe.get_single(SETTINGS_DOCTYPE)

    status = settings.get("tsp_last_run_status")
    started_at = settings.get("tsp_last_run_at")
    if status == "RUNNING" and started_at:
        elapsed = (now_datetime() - frappe.utils.get_datetime(started_at)).total_seconds() / 60
        if elapsed < LOCK_STALE_MINUTES:
            raise ValueError(
                "A weekly extract is currently running. Try again once it finishes "
                "(or wait a few minutes if it looks stuck)."
            )

    parquet_dir_value = settings.get("tsp_parquet_dir")
    base = _resolve_parquet_base(parquet_dir_value)

    if not base.exists():
        return {"status": "noop", "message": f"{base} does not exist, nothing to clear."}

    cleared = []

    live = base / LIVE_DIRNAME
    if _safe_rename_and_remove(live):
        cleared.append(str(live))

    staging = base / STAGING_DIRNAME
    if _safe_rename_and_remove(staging):
        cleared.append(str(staging))

    previous = base / "live" / "teacher_reporting.previous"
    if _safe_rename_and_remove(previous):
        cleared.append(str(previous))

    missing_path = base / "missing.jsonl"
    if missing_path.exists():
        try:
            missing_path.unlink()
            cleared.append(str(missing_path))
        except Exception:
            frappe.log_error(frappe.get_traceback(), "teacher_reporting: missing.jsonl cleanup failed")

    for tmp_leftover in base.glob("**/*.tmp"):
        try:
            tmp_leftover.unlink()
            cleared.append(str(tmp_leftover))
        except Exception:
            # Best-effort only -- a stray .tmp file left behind by an
            # interrupted write is harmless clutter, not a correctness
            # issue, so a failure here should not fail the whole clear.
            pass

    log_line = f"{now_datetime()} — CACHE CLEARED by {frappe.session.user} ({len(cleared)} paths)"
    existing = settings.get("tsp_last_run_log") or ""
    lines = (existing.splitlines() + [log_line])[-50:]
    settings.tsp_last_run_log = "\n".join(lines)
    settings.save(ignore_permissions=True)
    frappe.db.commit()

    return {"status": "success", "cleared": cleared, "count": len(cleared)}
