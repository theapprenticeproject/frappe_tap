# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

from tap_lms.onboarding.bulk_student_registration import run_import


STUDENT_BULK_IMPORT_TIMEOUT_SECONDS = 7200
STUDENT_BULK_IMPORT_STOP_BEFORE_TIMEOUT_SECONDS = 600
STUDENT_BULK_IMPORT_JOB_NAME_LENGTH = 140


class StudentBulkImportJob(Document):
    pass


def _set_glific_contact_files(docname: str, files: list[dict]) -> None:
    doc = frappe.get_doc("Student Bulk Import Job", docname)
    doc.set("glific_contact_files", [])
    for file_row in files:
        doc.append("glific_contact_files", {
            "file_name": str(file_row.get("file_name") or ""),
            "file_path": str(file_row.get("file_path") or ""),
            "row_count": int(file_row.get("row_count") or 0),
        })
    doc.save(ignore_permissions=True)


def _parse_tab_names(tab_names_json: str) -> list[str]:
    try:
        parsed = json.loads(tab_names_json or "[]")
    except Exception as exc:
        raise ValueError(f"Invalid tab_names_json: {exc}") from exc
    if not isinstance(parsed, list) or not parsed or not all(isinstance(item, str) and item.strip() for item in parsed):
        raise ValueError("tab_names_json must be a non-empty JSON array of strings")
    return [item.strip() for item in parsed]


def _append_log(docname: str, message: str) -> None:
    timestamp = frappe.utils.now_datetime().strftime("%Y-%m-%d %H:%M:%S")
    existing = frappe.db.get_value("Student Bulk Import Job", docname, "processing_log")
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
        "Student Bulk Import Job",
        docname,
        "processing_log",
        json.dumps({"entries": log_entries}, ensure_ascii=True, indent=2),
        update_modified=False,
    )


def _update_progress(docname: str, payload: dict) -> None:
    updates: dict[str, object] = {}
    summary = payload.get("summary") or {}
    if "batches_processed" in summary:
        updates["batches_processed"] = int(summary["batches_processed"] or 0)
    if "effective_rows" in payload:
        updates["effective_rows"] = int(payload["effective_rows"] or 0)
    if "failed_rows" in summary:
        updates["failed_rows"] = int(summary["failed_rows"] or 0)
    if "failed_rows_file_url" in summary:
        updates["failed_rows_file_url"] = str(summary["failed_rows_file_url"] or "")
    if "remaining_rows" in summary:
        updates["remaining_rows"] = str(summary["remaining_rows"] or "")
    if "drive_folder_url" in summary:
        updates["drive_folder_url"] = str(summary["drive_folder_url"] or "")
    if payload.get("event") == "completed":
        updates["summary_json"] = json.dumps(summary, indent=2, sort_keys=True)
        updates["elapsed"] = str(summary.get("elapsed") or "")
    if updates:
        frappe.db.set_value("Student Bulk Import Job", docname, updates, update_modified=False)


def _set_job_state(docname: str, **updates) -> None:
    if "processing_log" in updates and not isinstance(updates["processing_log"], str):
        updates["processing_log"] = json.dumps(
            updates["processing_log"],
            ensure_ascii=True,
            indent=2,
        )
    frappe.db.set_value("Student Bulk Import Job", docname, updates, update_modified=False)


def _create_and_start_continuation_job(
    source_job: StudentBulkImportJob,
    remaining_rows_url: str,
) -> dict:
    continuation_name = frappe.db.get_value(
        "Student Bulk Import Job",
        source_job.name,
        "continuation_job",
    )
    if continuation_name and frappe.db.exists("Student Bulk Import Job", continuation_name):
        continuation = frappe.get_doc("Student Bulk Import Job", continuation_name)
    else:
        timestamp = frappe.utils.now_datetime().strftime("%Y%m%d_%H%M%S")
        continuation_job_name = (
            f"{source_job.job_name} - continuation {timestamp}"
        )[:STUDENT_BULK_IMPORT_JOB_NAME_LENGTH]
        continuation = frappe.get_doc({
            "doctype": "Student Bulk Import Job",
            "job_name": continuation_job_name,
            "spreadsheet_url": remaining_rows_url,
            "tab_names_json": source_job.tab_names_json,
            "batch_size": int(source_job.batch_size or 100),
            "sample_test": 0,
            "status": "Draft",
            "continued_from": source_job.name,
        })
        continuation.insert(ignore_permissions=True)
        frappe.db.set_value(
            "Student Bulk Import Job",
            source_job.name,
            "continuation_job",
            continuation.name,
            update_modified=False,
        )

    if continuation.status not in {"Queued", "Processing", "Completed"}:
        queued = start_student_bulk_import_job(continuation.name)
    else:
        queued = {"status": continuation.status}
    return {
        "name": continuation.name,
        "status": queued.get("status") or continuation.status,
        "job_id": queued.get("job_id") or "",
    }


@frappe.whitelist()
def start_student_bulk_import_job(docname: str) -> dict:
    doc = frappe.get_doc("Student Bulk Import Job", docname)
    if doc.status in {"Queued", "Processing"}:
        frappe.throw("This student bulk import job is already queued or processing.")

    _parse_tab_names(doc.tab_names_json)
    import_date = frappe.utils.getdate(doc.started_at) if doc.started_at else frappe.utils.today()

    _set_job_state(
        docname,
        status="Queued",
        started_at=None,
        completed_at=None,
        elapsed="",
        batches_processed=0,
        effective_rows=0,
        failed_rows=0,
        failed_rows_file_url="",
        remaining_rows="",
        drive_folder_url="",
        continuation_job="",
        summary_json="",
        last_error="",
        processing_log={"entries": []},
    )
    _set_glific_contact_files(docname, [])
    frappe.db.commit()

    job = frappe.enqueue(
        "tap_lms.tap_lms.doctype.student_bulk_import_job.student_bulk_import_job.run_student_bulk_import_job",
        queue="long",
        timeout=STUDENT_BULK_IMPORT_TIMEOUT_SECONDS,
        job_name=f"student_bulk_import_job_{docname}",
        docname=docname,
        import_date=str(import_date),
    )
    return {"job_id": job.id, "status": "Queued"}


def run_student_bulk_import_job(docname: str, import_date: str | None = None) -> dict:
    doc = frappe.get_doc("Student Bulk Import Job", docname)
    started_at = frappe.utils.now_datetime()

    try:
        tab_names = _parse_tab_names(doc.tab_names_json)
        _set_job_state(
            docname,
            status="Processing",
            started_at=started_at,
            completed_at=None,
            elapsed="",
            last_error="",
        )
        frappe.db.commit()

        def log_fn(message: str) -> None:
            _append_log(docname, message)

        def progress_fn(payload: dict) -> None:
            _update_progress(docname, payload)

        summary = run_import(
            spreadsheet_url=doc.spreadsheet_url,
            tab_names=tab_names,
            sample_test=int(doc.sample_test or 0),
            batch_size=int(doc.batch_size or 100),
            import_user=doc.owner or "Administrator",
            job_name=doc.job_name or doc.name,
            import_date=import_date,
            timeout_seconds=STUDENT_BULK_IMPORT_TIMEOUT_SECONDS,
            stop_before_timeout_seconds=STUDENT_BULK_IMPORT_STOP_BEFORE_TIMEOUT_SECONDS,
            log_fn=log_fn,
            progress_fn=progress_fn,
        )

        continuation = None
        if summary.get("remaining_rows") and int(summary.get("remaining_rows_count") or 0) > 0:
            continuation = _create_and_start_continuation_job(
                doc,
                str(summary["remaining_rows"]),
            )
            summary["continuation_job"] = continuation["name"]
            summary["continuation_job_status"] = continuation["status"]
            summary["continuation_queue_job_id"] = continuation["job_id"]
            _append_log(
                docname,
                "[student-import] continuation queued "
                f"job={continuation['name']} queue_job_id={continuation['job_id']}",
            )

        _set_glific_contact_files(docname, summary.get("glific_contact_files") or [])
        _set_job_state(
            docname,
            status="Completed",
            completed_at=frappe.utils.now_datetime(),
            elapsed=str(summary.get("elapsed") or ""),
            remaining_rows=str(summary.get("remaining_rows") or ""),
            drive_folder_url=str(summary.get("drive_folder_url") or ""),
            continuation_job=(continuation or {}).get("name") or "",
            summary_json=json.dumps(summary, indent=2, sort_keys=True),
            last_error="",
        )
        frappe.db.commit()
        return summary
    except Exception:
        frappe.db.rollback()
        error_message = frappe.get_traceback()
        _append_log(docname, f"[student-import] failed\n{error_message}")
        _set_job_state(
            docname,
            status="Failed",
            completed_at=frappe.utils.now_datetime(),
            last_error=error_message,
        )
        frappe.db.commit()
        raise
