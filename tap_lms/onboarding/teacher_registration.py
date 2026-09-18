import random
import time

import frappe
from tap_lms.onboarding.utils import (
    _get_all_school_rows,
    _get_all_state_district_rows,
    _enqueue_glific_contact_sync,
    _ensure_teacher_enrollment,
    _get_language_id_to_name,
    _get_language_name_to_id,
    _get_latest_enrollment,
    _get_latest_school_batch_id,
    _get_request_data,
    _get_school_course_vertical_names,
    _get_school_row_by_id,
    _get_school_row_from_input,
    _normalize_phone,
    _phone_for_response,
    _phone_filter,
    _require_valid_phone,
    _respond,
    _validate_api_key_or_respond,
    _validate_phone,
)
from tap_lms.utils.api_failures import log_api_failure
from tap_lms.utils.glific_timeout import log_glific_timeout

try:
    import psycopg2.errors as pg_errors
except Exception:
    class _FallbackPgErrors:
        class SerializationFailure(Exception):
            pass

        class DeadlockDetected(Exception):
            pass

        class InFailedSqlTransaction(Exception):
            pass

    pg_errors = _FallbackPgErrors()


TEACHER_WRITE_MAX_RETRIES = 3
TEACHER_WRITE_RETRY_BACKOFFS = (0.05, 0.10, 0.20)
TRANSIENT_DB_CONFLICT_SNIPPETS = (
    "could not serialize access",
    "deadlock detected",
    "current transaction is aborted",
)


def _iter_exception_chain(exc):
    seen = set()
    current = exc
    while current and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)


def _is_transient_db_conflict(exc):
    transient_classes = (
        pg_errors.SerializationFailure,
        pg_errors.DeadlockDetected,
        pg_errors.InFailedSqlTransaction,
    )
    for current in _iter_exception_chain(exc):
        if isinstance(current, transient_classes):
            return True
        message = str(current or "").lower()
        if any(snippet in message for snippet in TRANSIENT_DB_CONFLICT_SNIPPETS):
            return True
    return False


def _sleep_before_teacher_write_retry(attempt):
    delay = TEACHER_WRITE_RETRY_BACKOFFS[
        min(attempt, len(TEACHER_WRITE_RETRY_BACKOFFS) - 1)
    ]
    time.sleep(delay + random.uniform(0, 0.02))


def _run_teacher_write_with_retry(operation, data, write_fn):
    for attempt in range(TEACHER_WRITE_MAX_RETRIES + 1):
        try:
            write_fn()
            return
        except Exception as exc:
            frappe.db.rollback()
            if (
                _is_transient_db_conflict(exc)
                and attempt < TEACHER_WRITE_MAX_RETRIES
            ):
                _sleep_before_teacher_write_retry(attempt)
                continue

            log_api_failure(operation, data, frappe.get_traceback())
            frappe.log_error(frappe.get_traceback(), f"{operation} failed")
            _respond(500, {"status": "failure", "message": str(exc)})
            return


@frappe.whitelist(allow_guest=True)
def list_state_districts():
    data = _get_request_data()
    try:
        if not _validate_api_key_or_respond(data.get("api_key")):
            return

        locations = _get_all_state_district_rows()
        states = sorted({location["state"] for location in locations})
        _respond(200, {"states": states, "districts": locations})
    except frappe.ValidationError:
        frappe.db.rollback()
        log_api_failure("list_state_districts", data, frappe.get_traceback())
        raise
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("list_state_districts", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "list_state_districts failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def list_school_details(state_name=None, district_name=None):
    data = _get_request_data()
    try:
        api_key = data.get("api_key")

        if not _validate_api_key_or_respond(api_key):
            return

        state_name = str(state_name or data.get("state_name") or "").strip()
        district_name = str(district_name or data.get("district_name") or "").strip()
        if not state_name or not district_name:
            _respond(400, {
                "status": "failure",
                "message": "state_name and district_name are required",
            })
            return

        schools = _get_all_school_rows(state_name, district_name)
        _respond(200, {"schools": schools})
    except frappe.ValidationError:
        frappe.db.rollback()
        log_api_failure("list_school_details", data, frappe.get_traceback())
        raise
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("list_school_details", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "list_school_details failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def get_teacher_courses(school_id=None, grade=None):
    data = _get_request_data()
    try:
        school_id = str(school_id or data.get("school_id") or "").strip()
        grade = str(grade or data.get("grade") or "").strip()

        if not school_id or not grade:
            _respond(400, {
                "status": "failure",
                "message": "school_id and grade are required.",
            })
            return

        if not _get_school_row_by_id(school_id):
            _respond(404, {"status": "failure", "message": "School not found"})
            return

        school_batch_data = _get_school_course_vertical_names(school_id, grade)
        course_names = school_batch_data.get("course_names") or []
        response = {"num_course": len(course_names)}
        for index, course_name in enumerate(course_names, start=1):
            response[f"course_{index}"] = course_name

        _respond(200, response)
        return
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("get_teacher_courses", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "get_teacher_courses failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def check_teacher_exists():
    data = _get_request_data()
    try:
        phone = _normalize_phone(data.get("phone"))

        if not _validate_phone(phone):
            _respond(
                400,
                {"exists": False, "message": "Phone must be exactly 10 digits or 12 digits starting with 91"},
            )
            return

        exists = bool(frappe.db.exists("Teacher", _phone_filter("phone_number", phone)))
        _respond(200, {"exists": exists})
    except frappe.ValidationError:
        frappe.db.rollback()
        log_api_failure("check_teacher_exists", data, frappe.get_traceback())
        raise
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("check_teacher_exists", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "check_teacher_exists failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def get_teacher_details():
    data = _get_request_data()
    try:
        phone = _normalize_phone(data.get("phone"))

        if not _validate_phone(phone):
            _respond(400, {"message": "Phone must be exactly 10 digits or 12 digits starting with 91"})
            return

        teacher = frappe.db.get_value(
            "Teacher",
            _phone_filter("phone_number", phone),
            [
                "name",
                "first_name",
                "last_name",
                "phone_number",
                "school_id",
                "teacher_role",
                "language",
            ],
            as_dict=True,
        )

        if not teacher:
            _respond(404, {"message": "Teacher not found"})
            return

        school_row = _get_school_row_by_id(teacher.school_id) if teacher.school_id else None
        name_parts = [
            part for part in (
                str(teacher.first_name or "").strip(),
                str(teacher.last_name or "").strip(),
            )
            if part
        ]
        payload = {
            "name": " ".join(name_parts),
            "phone": _phone_for_response(teacher.phone_number),
            "state": school_row["state"] if school_row else "",
            "district": school_row["district"] if school_row else "",
            "city": school_row["city"] if school_row else "",
            "school": school_row["school_name"] if school_row else "",
            "role": teacher.teacher_role or "",
            "language": _get_language_id_to_name(teacher.language),
        }
        _respond(200, payload)
    except frappe.ValidationError:
        frappe.db.rollback()
        log_api_failure("get_teacher_details", data, frappe.get_traceback())
        raise
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("get_teacher_details", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "get_teacher_details failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def update_teacher_details():
    data = _get_request_data()

    _run_teacher_write_with_retry(
        "update_teacher_details",
        data,
        lambda: _update_teacher_details_once(data),
    )


def _update_teacher_details_once(data):
    phone, phone_error = _require_valid_phone(data.get("phone"))
    if phone_error:
        _respond(phone_error["code"], phone_error["payload"])
        return

    school_value = data.get("school")
    school_row = _get_school_row_from_input(school_value) if school_value else None
    if school_value and not school_row:
        _respond(404, {"status": "failure", "message": "School not found"})
        return

    teacher_name = frappe.db.get_value("Teacher", _phone_filter("phone_number", phone), "name")
    if not teacher_name:
        _respond(404, {"status": "failure", "message": "Teacher not found"})
        return

    teacher = frappe.get_doc("Teacher", teacher_name)
    received_name = (data.get("name") or "").strip()
    if received_name:
        teacher.first_name = received_name
        teacher.last_name = ""
    teacher.phone_number = phone
    teacher.teacher_role = data.get("role") or teacher.teacher_role
    language_id, language_error = _get_language_name_to_id(data.get("language"))
    if language_error:
        _respond(language_error["code"], language_error["payload"])
        return
    teacher.language = language_id or teacher.language
    if school_row:
        teacher.school_id = school_row["school_id"]
        teacher.state = school_row["state_id"]
        teacher.teacher_batch = _get_latest_school_batch_id(school_row["school_id"])
        if teacher.teacher_batch:
            _ensure_teacher_enrollment(teacher, school_row, teacher.teacher_batch)

    teacher.save(ignore_permissions=True)
    _enqueue_glific_contact_sync("Teacher", teacher.name)
    frappe.db.commit()
    _respond(200, {"status": "success", "message": "Teacher details updated successfully."})


@frappe.whitelist(allow_guest=True)
def create_teacher_web():
    data = _get_request_data()

    _run_teacher_write_with_retry(
        "create_teacher_web",
        data,
        lambda: _create_teacher_web_once(data),
    )


def _create_teacher_web_once(data):
    if not _validate_api_key_or_respond(data.get("api_key")):
        return

    phone, phone_error = _require_valid_phone(data.get("phone"))
    if phone_error:
        _respond(phone_error["code"], phone_error["payload"])
        return

    teacher_name = (data.get("name") or "").strip()
    school_value = (data.get("school") or "").strip()
    if not teacher_name or not school_value:
        _respond(400, {
            "status": "failure",
            "message": "Missing required field: name or school",
        })
        return

    if frappe.db.exists("Teacher", _phone_filter("phone_number", phone)):
        _respond(409, {
            "status": "failure",
            "message": "A teacher with this phone number already exists",
        })
        return

    school_row = _get_school_row_from_input(school_value)
    if not school_row:
        _respond(404, {"status": "failure", "message": "School not found"})
        return

    language_id, language_error = _get_language_name_to_id(data.get("language"))
    if language_error:
        _respond(language_error["code"], language_error["payload"])
        return

    teacher = frappe.get_doc(
        {
            "doctype": "Teacher",
            "first_name": teacher_name,
            "last_name": "",
            "gender": (data.get("gender") or "").strip(),
            "phone_number": phone,
            "teacher_role": (data.get("role") or "").strip(),
            "language": language_id,
            "school_id": school_row["school_id"],
            "state": school_row["state_id"],
            "teacher_batch": _get_latest_school_batch_id(school_row["school_id"]),
        }
    )
    if teacher.teacher_batch:
        _ensure_teacher_enrollment(teacher, school_row, teacher.teacher_batch)
    teacher.insert(ignore_permissions=True)
    _enqueue_glific_contact_sync("Teacher", teacher.name)
    frappe.db.commit()

    _respond(200, {
        "status": "success",
        "message": "Teacher created successfully.",
        "teacher_id": teacher.name,
    })
    return


@frappe.whitelist(allow_guest=True)
@log_glific_timeout()
def teacher_whatsapp_response(phone_number):
    _run_teacher_write_with_retry(
        "teacher_whatsapp_response",
        {"phone_number": phone_number},
        lambda: _teacher_whatsapp_response_once(phone_number),
    )


def _teacher_whatsapp_response_once(phone_number):
    phone, phone_error = _require_valid_phone(phone_number)
    if phone_error:
        _respond(phone_error["code"], phone_error["payload"])
        return

    teacher_name = frappe.db.get_value("Teacher", _phone_filter("phone_number", phone), "name")
    if not teacher_name:
        _respond(404, {"status": "failure", "message": "Teacher not found"})
        return

    teacher = frappe.get_doc("Teacher", teacher_name)
    latest_enrollment = _get_latest_enrollment(teacher)
    if not latest_enrollment:
        _respond(404, {"status": "failure", "message": "Teacher enrollment not found"})
        return

    school_id = latest_enrollment.school or teacher.school_id
    school_row = _get_school_row_by_id(school_id) if school_id else None

    latest_enrollment.whatsapp_response = 1
    teacher.save(ignore_permissions=True)
    frappe.db.commit()

    response_payload = {
        "student_registration_url": (
            f"http://registration.theapprenticeproject.org/student/"
            f"{school_id}"
        ),
    }
    if (school_row or {}).get("city") not in {"DoE Zone 27", "DoE Zone 28"}:
        response_payload["student_consent_url"] = (
            f"https://api.whatsapp.com/send?phone=918454812392&text=tapschool:"
            f"{school_id}"
        )

    _respond(200, response_payload)
    return
