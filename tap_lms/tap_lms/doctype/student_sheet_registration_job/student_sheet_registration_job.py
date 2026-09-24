# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

from tap_lms.onboarding.student_sheet_registration import (
    prepare_student_sheet_registration,
    upload_prepared_student_sheet_registration,
)


class StudentSheetRegistrationJob(Document):
    pass


RUNNING_STATUSES = {"Preparing", "Uploading"}
CRON_LOG_DOCTYPE = "Student Sheet Registration Cron Log"
PREPARE_JOB_TIMEOUT_SECONDS = 7200
UPLOAD_JOB_TIMEOUT_SECONDS = 86400
DAILY_STUDENT_SHEET_REGISTRATION_JOB_METHOD = (
    "tap_lms.tap_lms.doctype.student_sheet_registration_job."
    "student_sheet_registration_job.run_daily_student_sheet_registration_job"
)


def _get_running_job() -> str | None:
    jobs = frappe.get_all(
        "Student Sheet Registration Job",
        filters={"status": ["in", sorted(RUNNING_STATUSES)]},
        pluck="name",
        order_by="modified desc",
        limit_page_length=1,
    )
    return jobs[0] if jobs else None


def _create_daily_job() -> str:
    now = frappe.utils.now_datetime()
    doc = frappe.get_doc({
        "doctype": "Student Sheet Registration Job",
        "job_name": f"Daily Student Sheet Registration {now.strftime('%Y-%m-%d %H:%M')}",
        "status": "Preparing",
        "started_at": now,
        "completed_at": None,
        "raw_rows": 0,
        "prepared_rows": 0,
        "uploaded_rows": 0,
        "failed_rows": 0,
        "duplicate_rows": 0,
        "skipped_done_rows": 0,
        "prepared_file_url": "",
        "failed_rows_file_url": "",
        "not_done_rows_file_url": "",
        "summary_json": "",
        "last_error": "",
        "prepared_rows_json": "[]",
        "processing_log": json.dumps({"entries": []}, ensure_ascii=True),
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def _cron_log_doctype_exists() -> bool:
    try:
        return bool(frappe.db.exists("DocType", CRON_LOG_DOCTYPE))
    except Exception:
        return False


def _create_cron_log(job_name: str = "", status: str = "Started", message: str = "") -> str:
    if not _cron_log_doctype_exists():
        return ""

    now = frappe.utils.now_datetime()
    doc = frappe.get_doc({
        "doctype": CRON_LOG_DOCTYPE,
        "run_name": f"Daily Student Sheet Registration {now.strftime('%Y-%m-%d %H:%M')}",
        "status": status,
        "student_sheet_registration_job": job_name,
        "started_at": now,
        "completed_at": now if status in {"Skipped", "Failed"} else None,
        "processed_rows": 0,
        "successful_rows": 0,
        "duplicate_rows": 0,
        "failed_rows": 0,
        "summary_json": "",
        "last_error": message if status == "Failed" else "",
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def _set_cron_log_state(logname: str, **updates) -> None:
    if not logname or not _cron_log_doctype_exists():
        return
    if "summary_json" in updates and not isinstance(updates["summary_json"], str):
        updates["summary_json"] = json.dumps(updates["summary_json"], ensure_ascii=True)
    frappe.db.set_value(CRON_LOG_DOCTYPE, logname, updates, update_modified=False)


def _cron_log_counts(summary: dict) -> dict:
    duplicate_rows = int(summary.get("duplicate_rows") or 0)
    raw_failed_rows = int(summary.get("failed_rows") or 0)
    successful_rows = (
        int(summary.get("uploaded_rows") or 0)
        + int(summary.get("skipped_done_rows") or 0)
    )
    return {
        "processed_rows": int(summary.get("raw_rows") or summary.get("prepared_rows") or 0),
        "successful_rows": successful_rows,
        "duplicate_rows": duplicate_rows,
        "failed_rows": max(raw_failed_rows - duplicate_rows, 0),
    }


def _cron_log_file_fields(summary: dict) -> dict:
    return {
        "glific_contact_file_url": _glific_contact_file_url(summary),
        "not_done_rows_file_url": str(summary.get("not_done_rows_file_url") or ""),
        "duplicate_phone_numbers_file_url": str(
            summary.get("duplicate_phone_numbers_file_url") or ""
        ),
        "other_failures_file_url": str(summary.get("other_failures_file_url") or ""),
    }


def _glific_contact_file_url(summary: dict) -> str:
    explicit_url = str(summary.get("glific_contact_file_url") or "").strip()
    if explicit_url:
        return explicit_url

    files = summary.get("glific_contact_files") or []
    if not isinstance(files, list):
        return ""

    urls = []
    for file_row in files:
        if not isinstance(file_row, dict):
            continue
        file_url = str(file_row.get("file_path") or file_row.get("file_url") or "").strip()
        if file_url:
            urls.append(file_url)
    return "\n".join(urls)


def _glific_contact_upload_error(summary: dict) -> str:
    if str(summary.get("glific_contact_upload_status") or "").strip().lower() != "failed":
        return ""
    return str(
        summary.get("glific_contact_upload_error")
        or "Glific contact update failed"
    ).strip()


def _complete_cron_log(logname: str, status: str, summary: dict, last_error: str = "") -> None:
    if not logname:
        return
    _set_cron_log_state(
        logname,
        status=status,
        completed_at=frappe.utils.now_datetime(),
        last_error=last_error,
        summary_json=json.dumps(summary or {}, indent=2, sort_keys=True),
        **_cron_log_counts(summary or {}),
        **_cron_log_file_fields(summary or {}),
    )


def _set_glific_contact_files(docname: str, files: list[dict]) -> None:
    doc = frappe.get_doc("Student Sheet Registration Job", docname)
    doc.set("glific_contact_files", [])
    for file_row in files:
        doc.append("glific_contact_files", {
            "file_name": str(file_row.get("file_name") or ""),
            "file_path": str(file_row.get("file_path") or ""),
            "row_count": int(file_row.get("row_count") or 0),
        })
    doc.save(ignore_permissions=True)


def _append_log(docname: str, message: str) -> None:
    timestamp = frappe.utils.now_datetime().strftime("%Y-%m-%d %H:%M:%S")
    existing = frappe.db.get_value("Student Sheet Registration Job", docname, "processing_log")
    if isinstance(existing, str) and existing.strip():
        try:
            existing = json.loads(existing)
        except Exception:
            existing = {"entries": [{"timestamp": None, "message": existing}]}

    if isinstance(existing, dict):
        raw_entries = existing.get("entries")
        log_entries = raw_entries if isinstance(raw_entries, list) else []
    elif isinstance(existing, list):
        log_entries = existing
    else:
        log_entries = []

    log_entries.append({
        "timestamp": timestamp,
        "message": message,
    })
    frappe.db.set_value(
        "Student Sheet Registration Job",
        docname,
        "processing_log",
        json.dumps({"entries": log_entries}, ensure_ascii=True),
        update_modified=False,
    )


def _set_job_state(docname: str, **updates) -> None:
    for key in ("processing_log", "summary_json", "prepared_rows_json"):
        if key in updates and not isinstance(updates[key], str):
            updates[key] = json.dumps(updates[key], ensure_ascii=True)
    frappe.db.set_value("Student Sheet Registration Job", docname, updates, update_modified=False)


def _set_summary_counts(docname: str, summary: dict) -> None:
    count_fields = (
        "raw_rows",
        "prepared_rows",
        "uploaded_rows",
        "failed_rows",
        "duplicate_rows",
        "skipped_done_rows",
    )
    updates = {
        fieldname: int(summary.get(fieldname) or 0)
        for fieldname in count_fields
        if fieldname in summary
    }
    if "prepared_file_url" in summary:
        updates["prepared_file_url"] = str(summary.get("prepared_file_url") or "")
    if "failed_rows_file_url" in summary:
        updates["failed_rows_file_url"] = str(summary.get("failed_rows_file_url") or "")
    if "not_done_rows_file_url" in summary:
        updates["not_done_rows_file_url"] = str(summary.get("not_done_rows_file_url") or "")
    frappe.db.set_value("Student Sheet Registration Job", docname, updates, update_modified=False)


def enqueue_daily_student_sheet_registration() -> dict:
    running_job = _get_running_job()
    if running_job:
        message = f"Daily student sheet registration skipped; job {running_job} is already running."
        frappe.logger("tap_lms.student_sheet_registration").info(message)
        cron_log = _create_cron_log(running_job, status="Skipped")
        frappe.db.commit()
        return {"status": "Skipped", "running_job": running_job}

    docname = _create_daily_job()
    cron_log = _create_cron_log(docname)
    try:
        _append_log(docname, "[student-sheet-registration] daily job created")
        frappe.db.commit()

        job = frappe.enqueue(
            DAILY_STUDENT_SHEET_REGISTRATION_JOB_METHOD,
            queue="long",
            timeout=UPLOAD_JOB_TIMEOUT_SECONDS,
            job_name=f"student_sheet_registration_daily_{docname}",
            docname=docname,
            cron_log_name=cron_log,
        )
        _append_log(docname, f"[student-sheet-registration] daily job queued rq_job_id={job.id}")
        _set_cron_log_state(cron_log, status="Queued")
        frappe.db.commit()
        return {"job_id": job.id, "docname": docname, "status": "Preparing"}
    except Exception:
        frappe.db.rollback()
        error_message = frappe.get_traceback()
        _set_job_state(
            docname,
            status="Failed",
            completed_at=frappe.utils.now_datetime(),
            last_error=error_message,
        )
        _complete_cron_log(cron_log, "Failed", {}, error_message)
        frappe.db.commit()
        raise


@frappe.whitelist()
def start_prepare_student_sheet_registration_job(docname: str) -> dict:
    doc = frappe.get_doc("Student Sheet Registration Job", docname)
    if doc.status in RUNNING_STATUSES:
        frappe.throw("This student sheet registration job is already running.")

    _set_job_state(
        docname,
        status="Preparing",
        started_at=None,
        completed_at=None,
        raw_rows=0,
        prepared_rows=0,
        uploaded_rows=0,
        failed_rows=0,
        duplicate_rows=0,
        skipped_done_rows=0,
        prepared_file_url="",
        failed_rows_file_url="",
        not_done_rows_file_url="",
        summary_json="",
        last_error="",
        prepared_rows_json="[]",
        processing_log={"entries": []},
    )
    _set_glific_contact_files(docname, [])
    frappe.db.commit()

    job = frappe.enqueue(
        "tap_lms.tap_lms.doctype.student_sheet_registration_job."
        "student_sheet_registration_job.run_prepare_student_sheet_registration_job",
        queue="long",
        timeout=PREPARE_JOB_TIMEOUT_SECONDS,
        job_name=f"student_sheet_registration_prepare_{docname}",
        docname=docname,
    )
    return {"job_id": job.id, "status": "Preparing"}


@frappe.whitelist()
def start_upload_student_sheet_registration_job(docname: str) -> dict:
    doc = frappe.get_doc("Student Sheet Registration Job", docname)
    if doc.status in RUNNING_STATUSES:
        frappe.throw("This student sheet registration job is already running.")
    if not doc.prepared_rows_json:
        frappe.throw("Prepare Data must be run before Complete Upload.")

    _set_job_state(
        docname,
        status="Uploading",
        started_at=frappe.utils.now_datetime(),
        completed_at=None,
        uploaded_rows=0,
        failed_rows=0,
        failed_rows_file_url="",
        not_done_rows_file_url="",
        summary_json="",
        last_error="",
    )
    _set_glific_contact_files(docname, [])
    frappe.db.commit()

    job = frappe.enqueue(
        "tap_lms.tap_lms.doctype.student_sheet_registration_job."
        "student_sheet_registration_job.run_upload_student_sheet_registration_job",
        queue="long",
        timeout=UPLOAD_JOB_TIMEOUT_SECONDS,
        job_name=f"student_sheet_registration_upload_{docname}",
        docname=docname,
    )
    return {"job_id": job.id, "status": "Uploading"}


def run_prepare_student_sheet_registration_job(docname: str) -> dict:
    started_at = frappe.utils.now_datetime()
    try:
        _set_job_state(
            docname,
            status="Preparing",
            started_at=started_at,
            completed_at=None,
            last_error="",
        )
        frappe.db.commit()

        result = prepare_student_sheet_registration(log_fn=lambda message: _append_log(docname, message))
        summary = result.get("summary") or {}
        _set_summary_counts(docname, summary)
        _set_job_state(
            docname,
            status="Prepared",
            completed_at=frappe.utils.now_datetime(),
            summary_json=json.dumps(summary, indent=2, sort_keys=True),
            prepared_rows_json=json.dumps(result.get("prepared_rows") or [], ensure_ascii=True),
            last_error="",
        )
        frappe.db.commit()
        return result
    except Exception:
        frappe.db.rollback()
        error_message = frappe.get_traceback()
        _append_log(docname, f"[student-sheet-registration] prepare_failed\n{error_message}")
        _set_job_state(
            docname,
            status="Failed",
            completed_at=frappe.utils.now_datetime(),
            last_error=error_message,
        )
        frappe.db.commit()
        raise


def run_upload_student_sheet_registration_job(docname: str) -> dict:
    doc = frappe.get_doc("Student Sheet Registration Job", docname)
    started_at = frappe.utils.now_datetime()
    try:
        _set_job_state(
            docname,
            status="Uploading",
            started_at=started_at,
            completed_at=None,
            last_error="",
        )
        frappe.db.commit()

        prepared_rows = json.loads(doc.prepared_rows_json or "[]")
        if not isinstance(prepared_rows, list):
            raise ValueError("prepared_rows_json must be a JSON array")

        result = upload_prepared_student_sheet_registration(
            prepared_rows,
            import_user=doc.owner or "Administrator",
            log_fn=lambda message: _append_log(docname, message),
        )
        _set_summary_counts(docname, result)
        _set_glific_contact_files(docname, result.get("glific_contact_files") or [])
        glific_upload_error = _glific_contact_upload_error(result)
        if glific_upload_error:
            _set_job_state(
                docname,
                status="Failed",
                completed_at=frappe.utils.now_datetime(),
                summary_json=json.dumps(result, indent=2, sort_keys=True),
                last_error=glific_upload_error,
            )
            frappe.db.commit()
            raise frappe.ValidationError(glific_upload_error)

        _set_job_state(
            docname,
            status="Completed",
            completed_at=frappe.utils.now_datetime(),
            summary_json=json.dumps(result, indent=2, sort_keys=True),
            last_error="",
        )
        frappe.db.commit()
        return result
    except Exception:
        frappe.db.rollback()
        error_message = frappe.get_traceback()
        _append_log(docname, f"[student-sheet-registration] upload_failed\n{error_message}")
        _set_job_state(
            docname,
            status="Failed",
            completed_at=frappe.utils.now_datetime(),
            last_error=error_message,
        )
        frappe.db.commit()
        raise


def run_daily_student_sheet_registration_job(docname: str, cron_log_name: str | None = None) -> dict:
    started_at = frappe.utils.now_datetime()
    latest_summary: dict = {}
    try:
        _set_cron_log_state(
            cron_log_name or "",
            status="Running",
            started_at=started_at,
            completed_at=None,
            last_error="",
        )
        _set_job_state(
            docname,
            status="Preparing",
            started_at=started_at,
            completed_at=None,
            raw_rows=0,
            prepared_rows=0,
            uploaded_rows=0,
            failed_rows=0,
            duplicate_rows=0,
            skipped_done_rows=0,
            prepared_file_url="",
            failed_rows_file_url="",
            not_done_rows_file_url="",
            summary_json="",
            last_error="",
            prepared_rows_json="[]",
        )
        _set_glific_contact_files(docname, [])
        frappe.db.commit()

        prepare_result = prepare_student_sheet_registration(
            log_fn=lambda message: _append_log(docname, message)
        )
        prepare_summary = prepare_result.get("summary") or {}
        latest_summary = dict(prepare_summary)
        prepared_rows = prepare_result.get("prepared_rows") or []
        _set_summary_counts(docname, prepare_summary)
        _set_job_state(
            docname,
            status="Prepared",
            summary_json=json.dumps(prepare_summary, indent=2, sort_keys=True),
            prepared_rows_json=json.dumps(prepared_rows, ensure_ascii=True),
            last_error="",
        )
        frappe.db.commit()

        ready_rows = [row for row in prepared_rows if row.get("prepare_status") == "Ready"]
        if not ready_rows:
            final_summary = dict(prepare_summary)
            final_summary.setdefault("uploaded_rows", 0)
            final_summary.setdefault("glific_contact_files", [])
            final_summary.setdefault("glific_contact_file_url", "")
            final_summary.setdefault("glific_contact_upload_status", "skipped")
            final_summary.setdefault("glific_contact_upload_error", "")
            final_summary.setdefault("glific_contact_upload_results", [])
            final_summary.setdefault("not_done_rows_file_url", "")
            latest_summary = dict(final_summary)
            _set_summary_counts(docname, final_summary)
            _set_glific_contact_files(docname, [])
            _set_job_state(
                docname,
                status="Completed",
                completed_at=frappe.utils.now_datetime(),
                summary_json=json.dumps(final_summary, indent=2, sort_keys=True),
                last_error="",
            )
            _complete_cron_log(cron_log_name or "", "Completed", final_summary)
            frappe.db.commit()
            return final_summary

        _set_job_state(
            docname,
            status="Uploading",
            completed_at=None,
            uploaded_rows=0,
            failed_rows=0,
            failed_rows_file_url="",
            not_done_rows_file_url="",
            last_error="",
        )
        _set_glific_contact_files(docname, [])
        frappe.db.commit()

        doc = frappe.get_doc("Student Sheet Registration Job", docname)
        upload_result = upload_prepared_student_sheet_registration(
            prepared_rows,
            import_user=doc.owner or "Administrator",
            log_fn=lambda message: _append_log(docname, message),
        )
        final_summary = dict(prepare_summary)
        final_summary.update(upload_result)
        latest_summary = dict(final_summary)
        _set_summary_counts(docname, final_summary)
        _set_glific_contact_files(docname, upload_result.get("glific_contact_files") or [])
        glific_upload_error = _glific_contact_upload_error(final_summary)
        if glific_upload_error:
            _set_job_state(
                docname,
                status="Failed",
                completed_at=frappe.utils.now_datetime(),
                summary_json=json.dumps(final_summary, indent=2, sort_keys=True),
                last_error=glific_upload_error,
            )
            _complete_cron_log(
                cron_log_name or "",
                "Failed",
                final_summary,
                glific_upload_error,
            )
            frappe.db.commit()
            raise frappe.ValidationError(glific_upload_error)

        _set_job_state(
            docname,
            status="Completed",
            completed_at=frappe.utils.now_datetime(),
            summary_json=json.dumps(final_summary, indent=2, sort_keys=True),
            last_error="",
        )
        _complete_cron_log(cron_log_name or "", "Completed", final_summary)
        frappe.db.commit()
        return final_summary
    except Exception:
        frappe.db.rollback()
        error_message = frappe.get_traceback()
        _append_log(docname, f"[student-sheet-registration] daily_failed\n{error_message}")
        _set_job_state(
            docname,
            status="Failed",
            completed_at=frappe.utils.now_datetime(),
            last_error=error_message,
        )
        _complete_cron_log(cron_log_name or "", "Failed", latest_summary, error_message)
        frappe.db.commit()
        raise
