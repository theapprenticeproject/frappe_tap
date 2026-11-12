"""In-process replacement for the REST FrappeClient used by the old import scripts."""

from __future__ import annotations

from typing import Any, Callable

import frappe
from frappe.utils import cint

from tap_lms.learning_unit_import.engine import slug


# Child tables are applied after the parent is inserted, matching the old scripts.
CHILD_TABLE_FIELDS = {
    "Assignment": {"assignment_translations", "submission_media_links"},
    "VideoClass": {"video_translations", "assessments"},
    "Quiz": {"questions"},
    "QuizQuestion": {"options", "question_translations"},
    "QuizOption": {"option_translations"},
    "LearningUnit": {"content_items"},
    "Course Level": {"learning_units"},
}


class FrappeWriter:
    def __init__(self, dry_run: bool, update_existing: bool, log: Callable[[str], None]):
        self.dry_run = dry_run
        self.update_existing = update_existing
        self.log = log
        self._dry_run_max_weeks: dict[str, int] = {}

    def upsert_by_filters(
        self,
        doctype: str,
        filters: list[list[Any]],
        payload: dict[str, Any],
        label: str,
        log_result: bool = True,
    ) -> tuple[str, str]:
        if self.dry_run:
            if log_result:
                self.log(f"DRY RUN upsert {doctype}: {label}")
            return f"DRY-RUN-{slug(label)}", "skipped"

        base_payload, child_payload = split_child_table_payload(doctype, payload)
        existing = frappe.get_all(doctype, filters=filters, pluck="name", limit=1)
        if existing:
            name = str(existing[0])
            if not self.update_existing:
                if log_result:
                    self.log(f"SKIP existing {doctype}: {name}")
                return name, "skipped"
            doc = frappe.get_doc(doctype, name)
            base_payload.pop("name", None)
            doc.update(base_payload)
            for fieldname, rows in child_payload.items():
                doc.set(fieldname, rows)
            doc.save()
            if log_result:
                self.log(f"UPDATED {doctype}: {name}")
            return name, "updated"

        doc = frappe.get_doc({"doctype": doctype, **base_payload})
        doc.insert()
        if child_payload:
            for fieldname, rows in child_payload.items():
                doc.set(fieldname, rows)
            doc.save()
        name = str(doc.name)
        if log_result:
            self.log(f"CREATED {doctype}: {name}")
        return name, "created"

    def update_doc(self, doctype: str, name: str, values: dict[str, Any]) -> None:
        doc = frappe.get_doc(doctype, name)
        doc.update(values)
        doc.save()

    def link_learning_unit_to_course_level(
        self,
        course_level_name: str,
        learning_unit_name: str,
        idx: int,
    ) -> tuple[str, int]:
        course_level = frappe.get_doc("Course Level", course_level_name)
        for row in course_level.get("learning_units") or []:
            if row.learning_unit == learning_unit_name:
                return "skipped", cint(row.week_no)

        assigned_week_no = max(
            [cint(row.week_no) for row in course_level.get("learning_units") or []] or [0]
        ) + 1

        course_level.append(
            "learning_units",
            {"learning_unit": learning_unit_name, "week_no": assigned_week_no, "idx": int(idx)},
        )
        course_level.save()
        return "created", assigned_week_no

    def get_next_course_level_week(
        self,
        course_level_name: str,
        course_level_filters: list[list[Any]],
    ) -> int:
        resolved_name = course_level_name
        if self.dry_run:
            existing = frappe.get_all("Course Level", filters=course_level_filters, pluck="name", limit=1)
            resolved_name = str(existing[0]) if existing else ""
            cache_key = resolved_name or repr(course_level_filters)
            if cache_key in self._dry_run_max_weeks:
                self._dry_run_max_weeks[cache_key] += 1
                return self._dry_run_max_weeks[cache_key]
        else:
            # The lock is held until the activity result is committed, preventing
            # concurrent imports from reserving the same next week.
            frappe.db.get_value("Course Level", resolved_name, "name", for_update=True)

        max_week = 0
        if resolved_name:
            course_level = frappe.get_doc("Course Level", resolved_name)
            max_week = max(
                [cint(row.week_no) for row in course_level.get("learning_units") or []] or [0]
            )
        next_week = max_week + 1
        if self.dry_run:
            self._dry_run_max_weeks[cache_key] = next_week
        return next_week

    def rollback(self) -> None:
        frappe.db.rollback()


def split_child_table_payload(doctype: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    child_fields = CHILD_TABLE_FIELDS.get(doctype) or set()
    base_payload = {}
    child_payload = {}
    for key, value in payload.items():
        if key in child_fields:
            child_payload[key] = value
        else:
            base_payload[key] = value
    return base_payload, child_payload
