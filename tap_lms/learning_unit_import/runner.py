"""Background job that executes a Learning Unit Import Run."""

from __future__ import annotations

import json
import traceback

import frappe
from frappe.utils import cint, now_datetime

from tap_lms.learning_unit_import.engine import (
    ActivityResult,
    ImportConfig,
    SourceTab,
    parse_unit_filters,
    run_import,
)
from tap_lms.learning_unit_import.sheets import GoogleSheetsClient
from tap_lms.learning_unit_import.writer import FrappeWriter


RUN_DOCTYPE = "Learning Unit Import Run"
SOURCE_DOCTYPE = "Learning Unit Import Source"
RESULT_DOCTYPE = "Learning Unit Import Run Result"
RUN_SOURCE_DOCTYPE = "Learning Unit Import Run Source"
PROGRESS_EVENT = "learning_unit_import_progress"
DONE_EVENT = "learning_unit_import_done"
JOB_TIMEOUT = 3 * 60 * 60


def enqueue_run(run_name: str) -> None:
    frappe.enqueue(
        "tap_lms.learning_unit_import.runner.execute_run",
        queue="long",
        timeout=JOB_TIMEOUT,
        enqueue_after_commit=True,
        run_name=run_name,
    )


def execute_run(run_name: str) -> None:
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    recorder = RunRecorder(run_name, dry_run=bool(cint(run.dry_run)))
    frappe.db.set_value(RUN_DOCTYPE, run_name, {"status": "Running", "started_at": now_datetime()})
    frappe.db.commit()

    source_statuses = []
    error_message = ""
    try:
        for row in run.sources:
            source_statuses.append(run_source(run, row, recorder))
    except Exception as exc:
        frappe.db.rollback()
        error_message = f"{exc}\n{traceback.format_exc()}"
        source_statuses.append("Failed")
    finally:
        if source_statuses and all(status == "Failed" for status in source_statuses):
            status = "Failed"
        elif any(status != "Completed" for status in source_statuses):
            status = "Completed With Errors"
        else:
            status = "Completed"
        failed_sources = [
            row.source for row, row_status in zip(run.sources, source_statuses) if row_status == "Failed"
        ]
        if failed_sources and not error_message:
            error_message = "Failed sources: " + ", ".join(failed_sources)
        recorder.flush_run({"status": status, "finished_at": now_datetime(), "error_message": error_message[:1000]})
        frappe.publish_realtime(
            DONE_EVENT,
            {"run": run_name, "status": status},
            doctype=RUN_DOCTYPE,
            docname=run_name,
        )


def run_source(run, row, recorder: "RunRecorder") -> str:
    """Import one source row. A failure is recorded on the row and doesn't stop later sources."""
    source_recorder = recorder.start_source(row)
    try:
        source = frappe.get_doc(SOURCE_DOCTYPE, row.source)
        config = build_config(run, row, source)
        source_recorder.log(
            f"Starting {'DRY RUN' if config.dry_run else 'import'} for source {source.name} "
            f"({config.source_type}), filters: {config.unit_filters or 'all rows'}"
        )
        sheets = GoogleSheetsClient(get_google_credentials(source.gcs_settings))
        writer = FrappeWriter(config.dry_run, config.update_existing, source_recorder.log)
        summary = run_import(config, sheets, writer, source_recorder)
        source_recorder.counts.update(summary)
        status = "Completed With Errors" if summary.get("errors") else "Completed"
        error_message = ""
    except Exception as exc:
        frappe.db.rollback()
        status = "Failed"
        error_message = str(exc)
        source_recorder.counts["errors"] = max(source_recorder.counts.get("errors", 0), 1)
        source_recorder.log(f"FAILED: {exc}\n{traceback.format_exc()}")

    source_recorder.finish(status, error_message)
    return status


def build_config(run, row, source) -> ImportConfig:
    return ImportConfig(
        source_type=source.source_type,
        spreadsheet_url=source.spreadsheet_url,
        course_level_program=source.course_level_program,
        master_sheet_name=source.master_sheet_name or "",
        source_vertical=source.source_vertical or "",
        source_tabs=[
            SourceTab(tab.sheet_name, tab.course or "", tab.level or "", tab.grade or "")
            for tab in source.source_tabs
        ],
        target_vertical=row.target_vertical or "",
        target_course=row.target_course or "",
        target_level=row.target_level or "",
        unit_filters=parse_unit_filters(row.unit_filters or ""),
        unit_match_mode=row.unit_match_mode or source.unit_match_mode,
        assignment_flow_spreadsheet_url=source.assignment_flow_spreadsheet_url or "",
        assignment_flow_sheet_name=source.assignment_flow_sheet_name or "",
        dry_run=bool(cint(run.dry_run)),
        update_existing=bool(cint(run.update_existing)),
        stop_on_first_error=bool(cint(run.stop_on_first_error)),
        difficulty_tier=source.difficulty_tier,
        default_status=source.default_status,
        link_learning_units_to_course_level=bool(cint(source.link_learning_units_to_course_level)),
    )


def get_google_credentials(gcs_settings_name: str) -> dict:
    credentials_json = frappe.db.get_value("GCS Settings", gcs_settings_name, "credentials_json")
    if not credentials_json:
        frappe.throw(f"GCS Settings {gcs_settings_name} has no credentials JSON")
    return json.loads(credentials_json)


COUNT_FIELDS = {
    "created": "created_count",
    "updated": "updated_count",
    "skipped": "skipped_count",
    "errors": "error_count",
}


def empty_counts() -> dict[str, int]:
    return {key: 0 for key in COUNT_FIELDS}


def count_values(counts: dict[str, int]) -> dict[str, int]:
    return {fieldname: counts.get(key, 0) for key, fieldname in COUNT_FIELDS.items()}


class RunRecorder:
    """Run-level totals and the shared activity results table.

    Writes happen between activity bundles, so each flush also commits the bundle
    that just finished.
    """

    def __init__(self, run_name: str, dry_run: bool):
        self.run_name = run_name
        self.dry_run = dry_run
        self.finished_counts = empty_counts()
        self.current: SourceRecorder | None = None
        self.result_idx = 0

    def start_source(self, row) -> "SourceRecorder":
        self.current = SourceRecorder(self, row)
        self.current.flush({"status": "Running"})
        return self.current

    def totals(self) -> dict[str, int]:
        totals = dict(self.finished_counts)
        if self.current:
            for key, value in self.current.counts.items():
                totals[key] = totals.get(key, 0) + value
        return totals

    def source_finished(self, source_recorder: "SourceRecorder") -> None:
        for key, value in source_recorder.counts.items():
            self.finished_counts[key] = self.finished_counts.get(key, 0) + value
        self.current = None

    def flush_run(self, extra: dict | None = None) -> None:
        frappe.db.set_value(RUN_DOCTYPE, self.run_name, {**count_values(self.totals()), **(extra or {})})
        frappe.db.commit()


class SourceRecorder:
    """Log, counts and status for one row of the run's Sources table."""

    def __init__(self, run_recorder: RunRecorder, row):
        self.run = run_recorder
        self.row_name = row.name
        self.source = row.source
        self.lines: list[str] = []
        self.counts = empty_counts()
        self.processed = 0

    def log(self, message: str) -> None:
        self.lines.append(f"[{now_datetime().strftime('%H:%M:%S')}] {message}")
        frappe.publish_realtime(
            PROGRESS_EVENT,
            {
                "run": self.run.run_name,
                "source": self.source,
                "message": message.splitlines()[0][:200],
                "processed": self.processed,
            },
            doctype=RUN_DOCTYPE,
            docname=self.run.run_name,
        )

    def activity_finished(self, result: ActivityResult, counts: dict[str, int]) -> None:
        self.processed += 1
        self.run.result_idx += 1
        for key, value in counts.items():
            self.counts[key] = self.counts.get(key, 0) + value

        activity = result.activity
        row = {
            "doctype": RESULT_DOCTYPE,
            "parent": self.run.run_name,
            "parenttype": RUN_DOCTYPE,
            "parentfield": "results",
            "idx": self.run.result_idx,
            "source": self.source,
            "sheet_name": activity.sheet_name,
            "row_number": activity.row_number,
            "course": activity.master.course,
            "level": activity.master.level,
            "unit_activity": activity.unit_activity,
            "activity_name": activity.activity_name,
            "week_no": result.week_no,
            "result_status": result.status,
            "message": result.message[:1000],
        }
        if not self.run.dry_run:
            row.update(
                {
                    "course_level": result.course_level,
                    "learning_unit": result.learning_unit,
                    "assignment": result.assignment,
                    "video_class": result.video_class,
                    "quiz": result.quiz,
                }
            )
        frappe.get_doc(row).db_insert()
        self.flush()

    def finish(self, status: str, error_message: str) -> None:
        self.flush({"status": status, "error_message": error_message[:1000]})
        self.run.source_finished(self)

    def flush(self, extra: dict | None = None) -> None:
        frappe.db.set_value(
            RUN_SOURCE_DOCTYPE,
            self.row_name,
            {"log": "\n".join(self.lines), **count_values(self.counts), **(extra or {})},
        )
        self.run.flush_run()
