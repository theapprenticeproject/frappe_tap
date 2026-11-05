import json
import re

import frappe
import requests
from frappe.utils import add_to_date, cint, now_datetime

SETTINGS_DOCTYPE = "BigQuery Settings"

DATE_FILTER_MODES = {"All", "Weekly", "Bi-monthly", "Monthly", "Custom"}

REQUIRED_SETTING_FIELDS = (
    "project_id", "credentials_json", "dataset_id", "contacts_table",
    "messages_table", "main_flow_name", "watched_flow_label",
    "shared_flow_label", "group_label_pattern", "webapp_url", "secret",
)

WEBAPP_TIMEOUT_SECONDS = 60

DEFAULT_MAX_BYTES_BILLED_GB = 10

LOCK_STALE_MINUTES = 10

_PLAUSIBLE_SCHOOL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,39}$")


def _looks_like_school_id(value):
    if not value:
        return False
    if "\n" in value or "\r" in value:
        return False
    return bool(_PLAUSIBLE_SCHOOL_ID_RE.match(value.strip()))


def get_poster_settings():
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
        "watched_flow_label": (settings.get("tsp_watched_flow_label") or "").strip(),
        "shared_flow_label": (settings.get("tsp_shared_flow_label") or "").strip(),

        "date_filter_mode": (settings.get("tsp_date_filter_mode") or "All").strip(),
        "custom_from": settings.get("tsp_custom_from"),
        "custom_to": settings.get("tsp_custom_to"),

        "webapp_url": (settings.get("tsp_webapp_url") or "").strip(),
        "secret": settings.get_password("tsp_secret", raise_exception=False),

        "max_query_gb": cint(settings.get("tsp_max_query_gb")) or DEFAULT_MAX_BYTES_BILLED_GB,

        "last_run_at": settings.get("tsp_last_run_at"),
    })


def _resolve_date_window(poster_settings):
    mode = poster_settings.date_filter_mode
    if mode not in DATE_FILTER_MODES:
        mode = "All"

    now = now_datetime()

    if mode == "All":
        return None, None
    if mode == "Weekly":
        return add_to_date(now, days=-7), now
    if mode == "Bi-monthly":
        return add_to_date(now, days=-15), now
    if mode == "Monthly":
        return add_to_date(now, days=-30), now
    if mode == "Custom":
        start = poster_settings.custom_from
        end = poster_settings.custom_to
        if not start or not end:
            raise ValueError(
                "date_filter_mode is Custom but tsp_custom_from/tsp_custom_to are not set"
            )
        return start, end

    return None, None


def _fully_qualified(poster_settings, table_name):
    return f"{poster_settings.project_id}.{poster_settings.dataset_id}.{table_name}"


def _required_fields_ok(poster_settings):
    missing = [f for f in REQUIRED_SETTING_FIELDS if not poster_settings.get(f)]
    return (not missing), missing


def get_bq_client(poster_settings):
    from google.cloud import bigquery
    from google.oauth2 import service_account

    if not poster_settings.credentials_json:
        raise ValueError("BigQuery Settings: Credentials JSON is not set")
    if not poster_settings.project_id:
        raise ValueError("BigQuery Settings: Project ID is not set")

    info = json.loads(poster_settings.credentials_json)
    credentials = service_account.Credentials.from_service_account_info(info)
    return bigquery.Client(project=poster_settings.project_id, credentials=credentials)


QUERY_TEMPLATE = """
WITH distinct_contacts AS (
    SELECT
        tab1.id,
        tab1.phone,
        TRIM(JSON_EXTRACT_SCALAR(tab1.raw_fields, '$.school_id.value')) AS school_id,
        JSON_EXTRACT_SCALAR(tab1.raw_fields, '$.school.value')          AS school,
        JSON_EXTRACT_SCALAR(tab1.raw_fields, '$.state.value')           AS state
    FROM `{contacts_table}` AS tab1
    INNER JOIN (
        SELECT id, MAX(updated_at) AS second_time
        FROM `{contacts_table}`
        GROUP BY id
    ) AS tab2
        ON tab1.id = tab2.id AND tab1.updated_at = tab2.second_time
    WHERE REGEXP_CONTAINS(tab1.group_labels, @group_label_pattern)
),

watched AS (
    SELECT DISTINCT contact_phone AS watched_phone
    FROM `{messages_table}`
    WHERE flow_name LIKE @main_flow_name
      AND flow_label LIKE @watched_flow_label
      {date_clause}
),

shared AS (
    SELECT DISTINCT contact_phone AS shared_phone
    FROM `{messages_table}`
    WHERE flow_name LIKE @main_flow_name
      AND flow_label LIKE @shared_flow_label
      {date_clause}
),

student_status AS (
    SELECT
        distinct_contacts.id,
        distinct_contacts.school_id,
        distinct_contacts.school,
        distinct_contacts.state,
        IF(watched.watched_phone IS NOT NULL, 1, 0) AS is_watched,
        IF(shared.shared_phone  IS NOT NULL, 1, 0) AS is_shared
    FROM distinct_contacts
    LEFT JOIN watched ON distinct_contacts.phone = watched.watched_phone
    LEFT JOIN shared  ON distinct_contacts.phone = shared.shared_phone
)

SELECT
    school_id                            AS school_id,
    ANY_VALUE(school)                    AS school_name,
    ANY_VALUE(state)                     AS state_name,
    COUNT(DISTINCT id)                   AS total_students_registered,
    SUM(is_watched)                      AS watched_video,
    COUNT(DISTINCT id) - SUM(is_watched) AS not_watched_video,
    SUM(is_shared)                       AS shared_work
FROM student_status
GROUP BY school_id
ORDER BY total_students_registered DESC
"""

_DATE_CLAUSE = "AND inserted_at BETWEEN @window_start AND @window_end"


def fetch_rows(poster_settings, client=None):
    from google.cloud import bigquery

    client = client or get_bq_client(poster_settings)

    start, end = _resolve_date_window(poster_settings)
    date_clause = _DATE_CLAUSE if start else ""

    query = QUERY_TEMPLATE.format(
        contacts_table=_fully_qualified(poster_settings, poster_settings.contacts_table),
        messages_table=_fully_qualified(poster_settings, poster_settings.messages_table),
        date_clause=date_clause,
    )

    params = [
        bigquery.ScalarQueryParameter(
            "group_label_pattern", "STRING", poster_settings.group_label_pattern
        ),
        bigquery.ScalarQueryParameter(
            "main_flow_name", "STRING", poster_settings.main_flow_name
        ),
        bigquery.ScalarQueryParameter(
            "watched_flow_label", "STRING", poster_settings.watched_flow_label
        ),
        bigquery.ScalarQueryParameter(
            "shared_flow_label", "STRING", poster_settings.shared_flow_label
        ),
    ]
    if start:
        params.append(bigquery.ScalarQueryParameter("window_start", "DATETIME", start))
        params.append(bigquery.ScalarQueryParameter("window_end", "DATETIME", end))

    max_bytes_billed = int(poster_settings.max_query_gb) * (1024 ** 3)

    job_config = bigquery.QueryJobConfig(
        query_parameters=params,
        maximum_bytes_billed=max_bytes_billed,
    )
    return list(client.query(query, job_config=job_config).result())


def resolve_districts(school_ids):
    missing = []

    safe_ids = []
    for school_id in sorted({sid for sid in school_ids if sid}):
        if _looks_like_school_id(school_id):
            safe_ids.append(school_id)
        else:
            missing.append({"school_id": school_id, "reason": "district_id_malformed"})

    if not safe_ids:
        return {}, missing

    try:
        schools = frappe.db.get_all(
            "School",
            filters={"keyword": ["in", safe_ids]},
            fields=["keyword", "district"],
        )
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "teacher_stats_poster: district lookup failed")
        for school_id in safe_ids:
            missing.append({"school_id": school_id, "reason": "district_unresolved"})
        return {}, missing

    school_by_id = {row.keyword: row for row in schools}

    district_ids = sorted({row.district for row in schools if row.district})
    valid_district_ids = set()
    if district_ids:
        try:
            valid_district_ids = {
                row.name for row in frappe.db.get_all(
                    "District", filters={"name": ["in", district_ids]}, fields=["name"]
                )
            }
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), "teacher_stats_poster: district validity check failed")
            valid_district_ids = set()

    district_by_id = {}
    for school_id in safe_ids:
        school = school_by_id.get(school_id)
        if not school:
            missing.append({"school_id": school_id, "reason": "no_school_record"})
            continue
        if not school.district:
            missing.append({"school_id": school_id, "reason": "district_unresolved"})
            continue
        if school.district not in valid_district_ids:
            missing.append({
                "school_id": school_id,
                "reason": "district_unresolved",
                "district_id": school.district,
            })
            continue
        district_by_id[school_id] = school.district

    return district_by_id, missing


def _acquire_lock():
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    status = settings.get("tsp_last_run_status")
    started_at = settings.get("tsp_last_run_at")

    if status == "RUNNING" and started_at:
        elapsed_minutes = (now_datetime() - frappe.utils.get_datetime(started_at)).total_seconds() / 60
        if elapsed_minutes < LOCK_STALE_MINUTES:
            return False

    settings.tsp_last_run_status = "RUNNING"
    settings.tsp_last_run_at = now_datetime()
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    return True


def _run_query_and_resolve(poster_settings):
    rows = fetch_rows(poster_settings)

    school_ids = [str(row["school_id"]) for row in rows if row["school_id"]]
    district_by_id, district_missing = resolve_districts(school_ids)

    result_rows = []
    missing = list(district_missing)

    for row in rows:
        school_id = str(row["school_id"] or "").strip()
        if not school_id:
            continue

        school_name = (row["school_name"] or "").strip()
        if not school_name:
            school_name = school_id
            missing.append({"school_id": school_id, "reason": "name_missing_in_source"})

        state_name = (row["state_name"] or "").strip()
        if not state_name:
            missing.append({"school_id": school_id, "reason": "state_missing_in_source"})

        district_name = district_by_id.get(school_id, "")

        result_rows.append({
            "school_id": school_id,
            "school_name": school_name,
            "district_name": district_name,
            "state_name": state_name,
            "total_students_registered": int(row["total_students_registered"] or 0),
            "watched_video": int(row["watched_video"] or 0),
            "not_watched_video": int(row["not_watched_video"] or 0),
            "shared_work": int(row["shared_work"] or 0),
        })

    result = {
        "generated_at": str(now_datetime()),
        "row_count": len(result_rows),
        "rows": result_rows,
        "missing_mappings": missing,
    }
    return result


def _push_to_webapp(poster_settings, result):
    response = requests.post(
        poster_settings.webapp_url,
        json=dict(result, secret=poster_settings.secret),
        headers={"X-TSP-API-Key": poster_settings.secret},
        timeout=WEBAPP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    try:
        ack = response.json()
    except ValueError:
        raise ValueError(f"Web App returned non-JSON response: {response.text[:300]}")

    if ack.get("status") != "ok":
        raise ValueError(f"Web App did not acknowledge success: {ack}")

    return ack


def _persist_run(status, result=None, missing_count=0, error=None):
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    settings.tsp_last_run_at = now_datetime()
    settings.tsp_last_run_status = status

    if status == "OK":
        log_line = (
            f"{now_datetime()} — OK rows={result['row_count']} "
            f"missing={missing_count}"
        )
    else:
        log_line = f"{now_datetime()} — ERROR: {error}"

    existing_log = settings.get("tsp_last_run_log") or ""
    lines = (existing_log.splitlines() + [log_line])[-50:]
    settings.tsp_last_run_log = "\n".join(lines)

    settings.save(ignore_permissions=True)
    frappe.db.commit()


def run_teacher_stats_poster():
    poster_settings = get_poster_settings()

    if not poster_settings.enabled:
        return {"status": "disabled", "message": "Teacher Stats Poster is not enabled"}

    ok, missing_fields = _required_fields_ok(poster_settings)
    if not ok:
        message = f"Missing settings: {', '.join(missing_fields)}"
        _persist_run("ERROR", error=message)
        return {"status": "failure", "message": message}

    if not _acquire_lock():
        return {"status": "skipped", "message": "A run is already in progress"}

    try:
        result = _run_query_and_resolve(poster_settings)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "teacher_stats_poster: query failed")
        _persist_run("ERROR", error="Query failed — see Error Log")
        return {"status": "failure", "message": "Query failed — see Error Log"}

    try:
        _push_to_webapp(poster_settings, result)
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "teacher_stats_poster: push to Web App failed")
        _persist_run("ERROR", error=f"Push to Web App failed: {exc}")
        return {"status": "failure", "message": f"Push to Web App failed: {exc}"}

    missing_count = len(result.get("missing_mappings") or [])
    _persist_run("OK", result=result, missing_count=missing_count)

    return {
        "status": "success",
        "row_count": result["row_count"],
        "missing_mappings": missing_count,
    }
