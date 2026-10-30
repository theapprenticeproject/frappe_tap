"""Webhook endpoints for the teacher submission flow.

Two calls, in conversation order:

1. start_submission()    phone + timestamp        -> returns submission_id
2. save_course_grade()   submission_id + details  -> fills in course and grade

Both follow the pattern used across tap_lms.onboarding:
read data -> validate -> look up -> write -> commit -> respond,
all wrapped in try/except with rollback and failure logging.
"""

import frappe
from frappe.utils import get_datetime, now_datetime

from tap_lms.onboarding.utils import (
    _get_request_data,
    _get_school_row_by_id,
    _phone_filter,
    _phone_for_response,
    _require_valid_phone,
    _respond,
)
from tap_lms.teacher_bot.images import attach_images, normalize_images
from tap_lms.utils.api_failures import log_api_failure


# ---------------------------------------------------------------------------
# 1. Save phone + timestamp, hand back an id
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def start_submission():
    """Open a submission and return its id.

    Expected body:
        phone          required
        submitted_at   optional ISO datetime from Glific; server time if absent
        images         optional; attach files in this same call. Accepts a list
                       of URLs or of objects. Useful when the whole form arrives
                       at once, so Glific needs only one webhook.

    Returns:
        submission_id, known, teacher_name, school_name, image_count

    An unregistered number still gets a row, flagged `is_unknown_number`.
    PRD section 5: never go silent, never refuse.
    """
    data = _get_request_data()
    try:
        phone, phone_error = _require_valid_phone(data.get("phone"))
        if phone_error:
            _respond(phone_error["code"], phone_error["payload"])
            return

        submitted_at, timestamp_error = _resolve_submitted_at(data.get("submitted_at"))
        if timestamp_error:
            _respond(400, {"status": "failure", "message": timestamp_error})
            return

        teacher = _get_teacher_by_phone(phone)

        doc = frappe.get_doc({
            "doctype": "Teacher Submission",
            "phone_number": phone,
            "teacher": teacher.name if teacher else None,
            "school_id": teacher.school_id if teacher else None,
            "is_unknown_number": 0 if teacher else 1,
            "submitted_at": submitted_at,
            "status": "Started",
        })

        # Optional: the caller may send files in this same call, so a Glific
        # flow that already has the URLs needs only one webhook.
        image_rows = normalize_images(data.get("images") or data.get("media"))
        if image_rows:
            attach_images(doc, image_rows)

        doc.insert(ignore_permissions=True)
        frappe.db.commit()


        _respond(200, {
            "status": "success",
            "submission_id": doc.name,
            "known": bool(teacher),
            "submitted_at": str(doc.submitted_at),
            "image_count": doc.image_count or 0,
        })
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("start_submission", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "start_submission failed")
        _respond(500, {"status": "failure", "message": str(exc)})


# ---------------------------------------------------------------------------
# 2. Attach course + grade to that id
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def save_course_grade():
    """Save course and grade against an existing submission.

    Expected body:
        submission_id  required, the value returned by start_submission
        course         required, a Course Verticals name e.g. "Arts"
        grade          required, "1".."12"
        section        required, free text e.g. "A"

    Safe to call twice with the same values: it overwrites rather than erroring,
    because Glific retries webhooks.
    """
    data = _get_request_data()
    try:
        submission_id = str(data.get("submission_id") or "").strip()
        if not submission_id:
            _respond(400, {"status": "failure", "message": "submission_id is required"})
            return

        if not frappe.db.exists("Teacher Submission", submission_id):
            _respond(404, {"status": "failure", "message": "Submission not found"})
            return

        course = str(data.get("course") or "").strip()
        grade = str(data.get("grade") or "").strip()
        section = str(data.get("section") or "").strip()
        if not course or not grade or not section:
            _respond(400, {"status": "failure", "message": "course, grade and section are required"})
            return

        if not frappe.db.exists("Course Verticals", course):
            _respond(400, {
                "status": "failure",
                "message": f"Unknown course: {course}",
                "allowed_values": frappe.get_all("Course Verticals", pluck="name"),
            })
            return

        if grade not in {str(number) for number in range(1, 13)}:
            _respond(400, {"status": "failure", "message": f"Invalid grade: {grade}"})
            return

        doc = frappe.get_doc("Teacher Submission", submission_id)
        doc.course = course
        doc.grade = grade
        doc.section = section
        doc.status = "Details Added"
        doc.save(ignore_permissions=True)
        frappe.db.commit()

        _respond(200, {
            "status": "success",
            "submission_id": doc.name,
            "submission_status": doc.status,
        })
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("save_course_grade", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "save_course_grade failed")
        _respond(500, {"status": "failure", "message": str(exc)})


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _get_teacher_by_phone(phone):
    """The phone number is the identity — she never types an ID (PRD section 5)."""
    teacher_name = frappe.db.get_value("Teacher", _phone_filter("phone_number", phone), "name")
    if not teacher_name:
        return None

    return frappe.db.get_value(
        "Teacher",
        teacher_name,
        ["name", "first_name", "last_name", "phone_number", "school_id"],
        as_dict=True,
    )


def _resolve_submitted_at(raw_value):
    """Use Glific's timestamp when sent, else server time.

    Returns (datetime, error_message). A malformed timestamp is rejected rather
    than silently replaced, so a broken flow shows up immediately.
    """
    if not raw_value:
        return now_datetime(), None

    try:
        return get_datetime(raw_value), None
    except Exception:
        return None, f"Invalid submitted_at: {raw_value}"
