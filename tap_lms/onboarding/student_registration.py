import random
import time

import frappe
from frappe.utils import now_datetime
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

from tap_lms.tap_lms.doctype.student.student import _reserve_next_student_name
from tap_lms.onboarding.utils import (
    _enqueue_glific_contact_sync,
    _get_course_level_label_for_grade,
    _get_language_id_to_name,
    _get_language_name_to_id,
    _get_latest_enrollment,
    _get_request_data,
    _get_school_course_vertical_names,
    _get_school_row_by_id,
    _phone_for_response,
    _phone_filter,
    _require_valid_phone,
    _respond,
)
from tap_lms.utils.api_failures import log_api_failure


ALLOWED_STUDENT_COURSE_NAMES = {
    "Arts",
    "Coding",
    "Science Lab",
    "Financial Literacy",
}

STUDENT_COURSE_NAME_ALIASES = {
    "Science": "Science Lab",
}

SCHOOL_STATE_LANGUAGE_MAP = {
    "MAHARASHTRA": "undetected",
    "PUNJAB": "Punjabi",
    "UTTAR PRADESH": "Hindi",
    "DELHI": "Hinglish",
}

SCHOOL_FLOW_REGISTRATION_CITIES = { }

CREATE_STUDENT_WEB_MAX_RETRIES = 3
CREATE_STUDENT_WEB_RETRY_BACKOFFS = (0.05, 0.10, 0.20)
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


def _sleep_before_create_student_retry(attempt):
    delay = CREATE_STUDENT_WEB_RETRY_BACKOFFS[
        min(attempt, len(CREATE_STUDENT_WEB_RETRY_BACKOFFS) - 1)
    ]
    time.sleep(delay + random.uniform(0, 0.02))


def _sync_student_series_counter():
    # Legacy meta `format:ST{########}` uses the empty-string series key,
    # while corrected meta `format:ST.########` uses the `ST` series key.
    # Keep both aligned during rollout so web inserts work before/after migrate.
    frappe.db.sql("""
        INSERT INTO "tabSeries" (name, current)
        VALUES ('ST', 0), ('', 0)
        ON CONFLICT (name) DO NOTHING
    """)
    frappe.db.sql("""
        WITH max_student AS (
            SELECT COALESCE(
                max(CASE
                    WHEN name ~ '^ST[0-9]{8}$' THEN substring(name from 3)::integer
                    ELSE 0
                END),
                0
            ) AS max_no
            FROM "tabStudent"
        )
        UPDATE "tabSeries" ts
           SET current = GREATEST(ts.current, ms.max_no)
         FROM max_student ms
         WHERE ts.name IN ('ST', '')
    """)


def _is_student_name_duplicate(exc: Exception) -> bool:
    message = str(exc or "")
    return (
        isinstance(exc, frappe.DuplicateEntryError)
        and "Student" in message
        and "ST" in message
    )


def _rebuild_student_doc(student):
    payload = student.as_dict()
    payload.pop("name", None)
    payload.pop("__islocal", None)
    payload.pop("owner", None)
    payload.pop("creation", None)
    payload.pop("modified", None)
    payload.pop("modified_by", None)
    payload.pop("docstatus", None)

    enrollments = []
    for row in payload.get("enrollment") or []:
        child = dict(row)
        child.pop("name", None)
        child.pop("parent", None)
        child.pop("parenttype", None)
        child.pop("parentfield", None)
        child.pop("idx", None)
        child.pop("__islocal", None)
        enrollments.append(child)
    payload["enrollment"] = enrollments
    return frappe.get_doc(payload)


def _insert_student_with_series_self_heal(student):
    for _attempt in range(3):
        try:
            student = _rebuild_student_doc(student)
            student.name = _reserve_next_student_name()
            old_in_import = getattr(frappe.flags, "in_import", False)
            frappe.flags.in_import = True
            try:
                student.insert(ignore_permissions=True)
            finally:
                frappe.flags.in_import = old_in_import
            return student
        except frappe.DuplicateEntryError as exc:
            if not _is_student_name_duplicate(exc):
                raise
            frappe.db.rollback()

    raise frappe.DuplicateEntryError(
        "Student",
        student.name,
        "Unable to allocate a unique Student ID after 3 attempts",
    )


def _normalize_student_course_name(course_name):
    course_name = (course_name or "").strip()
    return STUDENT_COURSE_NAME_ALIASES.get(course_name, course_name)


def _get_course_vertical_and_level(course_name, grade):
    course_vertical = frappe.db.get_value(
        "Course Verticals",
        {"name2": course_name},
        ["name", "name2"],
        as_dict=True,
    )
    if not course_vertical:
        return None, {
            "code": 404,
            "payload": {
                "status": "failure",
                "message": "Course vertical not found",
            },
        }

    level_label, level_error = _get_course_level_label_for_grade(grade)
    if level_error:
        return None, level_error
    return {
        "course_vertical": course_vertical,
        "level_label": level_label,
    }, None


def _get_school_language(state):
    return SCHOOL_STATE_LANGUAGE_MAP.get(state, "")


def _get_school_registration_type(city):
    return "flow" if (city).strip() in SCHOOL_FLOW_REGISTRATION_CITIES else "form"


def _upsert_student_consent(phone_number, school_id=None, whatsapp_consent=0):
    operation = "upsert_student_consent"
    phone_number, phone_error = _require_valid_phone(phone_number)
    if phone_error:
        error_context = {
            "operation": operation,
            "phone_number": phone_number,
            "school_id": school_id,
            "whatsapp_consent": int(whatsapp_consent or 0),
            "validation_error": phone_error["payload"]["message"],
        }
        log_api_failure(operation, error_context, phone_error["payload"]["message"])
        frappe.log_error(
            message=frappe.as_json(error_context),
            title="upsert_student_consent validation failed",
        )
        return
    school_id = str(school_id or "").strip() or None
    requested_consent = int(whatsapp_consent or 0)

    savepoint = "upsert_student_consent"
    frappe.db.savepoint(savepoint)
    try:
        consent_name = frappe.db.get_value(
            "Student Consent",
            _phone_filter("phone_number", phone_number),
            "name",
        )
        if consent_name:
            consent_doc = frappe.get_doc("Student Consent", consent_name)
            consent_doc.phone_number = phone_number
            consent_doc.school = school_id
            consent_doc.whatsapp_consent = max(
                int(consent_doc.whatsapp_consent or 0),
                requested_consent,
            )
            consent_doc.save(ignore_permissions=True)
            return

        consent_doc = frappe.get_doc(
            {
                "doctype": "Student Consent",
                "phone_number": phone_number,
                "school": school_id,
                "whatsapp_consent": requested_consent,
            }
        )
        consent_doc.insert(ignore_permissions=True)
    except Exception as exc:
        if _is_transient_db_conflict(exc):
            frappe.db.rollback()
            raise

        frappe.db.rollback(save_point=savepoint)
        error_context = {
            "operation": operation,
            "phone_number": phone_number,
            "school_id": school_id,
            "whatsapp_consent": requested_consent,
        }
        error_trace = frappe.get_traceback()
        log_api_failure(operation, error_context, error_trace)
        frappe.log_error(
            message=f"{frappe.as_json(error_context)}\n\n{error_trace}",
            title="upsert_student_consent failed",
        )


@frappe.whitelist(allow_guest=True)
@log_glific_timeout()
def verify_school_by_id(school_id, phone_number=None):
    try:
        school_id = str(school_id or "").strip()
        if not school_id:
            _respond(400, {"status": "failure", "message": "school_id is required"})
            return

        if phone_number:
            phone_number, phone_error = _require_valid_phone(phone_number)
            if phone_error:
                _respond(phone_error["code"], phone_error["payload"])
                return

        school_row = _get_school_row_by_id(school_id)
        if not school_row:
            _respond(404, {"status": "failure", "message": "School not found"})
            return

        if phone_number:
            frappe.enqueue(
                "tap_lms.onboarding.student_registration._upsert_student_consent",
                queue="default",
                phone_number=phone_number,
                school_id=school_id,
                whatsapp_consent=0,
            )

        _respond(
            200,
            {
                "status": "success",
                "school_id": school_row["school_id"],
                "school_name": school_row["school_name"],
                "state": school_row["state"],
                "district": school_row["district"],
                "city": school_row["city"],
                "school_langugage": _get_school_language(school_row["state"]),
                "registration_type": _get_school_registration_type(school_row["city"]),
                "student_registration_url": (
                    f"http://registration.theapprenticeproject.org/student/{school_row['school_id']}"
                ),
            },
        )
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure(
            "verify_school_by_id",
            {"school_id": school_id, "phone_number": phone_number},
            frappe.get_traceback(),
        )
        frappe.log_error(frappe.get_traceback(), "verify_school_by_id failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def create_student_web():
    data = _get_request_data()

    for attempt in range(CREATE_STUDENT_WEB_MAX_RETRIES + 1):
        try:
            _create_student_web_once(data)
            return
        except Exception as exc:
            frappe.db.rollback()
            if (
                _is_transient_db_conflict(exc)
                and attempt < CREATE_STUDENT_WEB_MAX_RETRIES
            ):
                _sleep_before_create_student_retry(attempt)
                continue

            log_api_failure("create_student_web", data, frappe.get_traceback())
            frappe.log_error(frappe.get_traceback(), "create_student_web failed")
            _respond(500, {"status": "failure", "message": str(exc)})
            return


def _create_student_web_once(data):
    phone, phone_error = _require_valid_phone(data.get("phone"))
    if phone_error:
        _respond(phone_error["code"], phone_error["payload"])
        return
    student_name = (data.get("student_name") or "").strip()
    school_id = (data.get("school_id") or "").strip()
    gender = (data.get("gender") or "").strip()
    grade = str(data.get("grade") or "").strip()

    required = [student_name, school_id, gender, grade, data.get("language")]
    if not all(required):
        _respond(400, {
            "status": "failure",
            "message": "school_id, student_name, phone, gender, grade and language are required.",
        })
        return

    school_row = _get_school_row_by_id(school_id)
    if not school_row:
        _respond(404, {"status": "failure", "message": "School not found"})
        return

    existing_student_name = frappe.db.get_value("Student", _phone_filter("phone", phone), "name")
    requested_language = (data.get("language") or "").strip()
    language_id, language_error = _get_language_name_to_id(requested_language)
    if language_error:
        _respond(language_error["code"], language_error["payload"])
        return

    if existing_student_name:
        student = frappe.get_doc("Student", existing_student_name)
        student.phone = phone
        student.language = language_id
        student.whatsapp_consent = 1
        response_school_row = (
            _get_school_row_by_id(student.school_id) if student.school_id else None
        )
        data_mismatch = {}
        existing_school_name = (
            response_school_row["school_name"] if response_school_row else ""
        )
        received_school_name = school_row["school_name"] if school_row else ""

        comparisons = {
            "school_name": (existing_school_name, received_school_name),
            "student_name": (student.name1 or "", student_name),
            "gender": (student.gender or "", gender),
            "grade": (student.grade or "", grade),
            "language": (_get_language_id_to_name(student.language), requested_language),
        }

        for field_name, (existing_value, received_value) in comparisons.items():
            if str(existing_value or "").strip() != str(received_value or "").strip():
                data_mismatch[field_name] = [existing_value, received_value]

        student.data_mismatch = data_mismatch
    else:
        student = frappe.get_doc(
            {
                "doctype": "Student",
                "name1": student_name,
                "phone": phone,
                "gender": gender,
                "school_id": school_id,
                "state": school_row["state_id"],
                "grade": grade,
                "language": language_id,
                "whatsapp_consent": 0,
                "joined_on": now_datetime().date(),
                "status": "active",
            }
        )
        response_school_row = school_row
        student.data_mismatch = {}

    school_batch_data = _get_school_course_vertical_names(school_id, grade)
    school_batch = str(school_batch_data.get("batch") or "").strip()
    if school_batch:
        level_label, _ = _get_course_level_label_for_grade(grade)
        enrollment_row = {
            "batch": school_batch,
            "vertical": school_batch_data["vertical"],
            "level": level_label or "",
            "grade": grade,
            "date_joining": now_datetime().date(),
            "school": school_id,
            "whatsapp_response": 0,
        }

        has_matching_enrollment = any(
            str(enrollment.batch or "").strip() == school_batch
            for enrollment in (student.get("enrollment") or [])
        )
        if not has_matching_enrollment:
            student.append("enrollment", enrollment_row)

    if existing_student_name:
        student.save(ignore_permissions=True)
    else:
        student = _insert_student_with_series_self_heal(student)

    _upsert_student_consent(phone, school_id=school_id, whatsapp_consent=1)
    _enqueue_glific_contact_sync("Student", student.name)
    frappe.db.commit()

    _respond(200, {
        "status": "success",
        "message": (
            "Student enrollment added successfully."
            if existing_student_name
            else "Student registered successfully."
        ),
        "school_name": (
            response_school_row["school_name"] if response_school_row else ""
        ),
        "student_name": student.name1,
        "phone": _phone_for_response(student.phone),
        "gender": student.gender,
        "grade": student.grade,
        "language": _get_language_id_to_name(student.language),
    })
    return


@frappe.whitelist(allow_guest=True)
@log_glific_timeout()
def student_whatsapp_response(phone_number):
    try:
        phone, phone_error = _require_valid_phone(phone_number)
        if phone_error:
            _respond(phone_error["code"], phone_error["payload"])
            return
        student_name = frappe.db.get_value("Student", _phone_filter("phone", phone), "name")
        if not student_name:
            _respond(404, {"status": "failure", "message": "Student not found"})
            return

        student = frappe.get_doc("Student", student_name)
        latest_enrollment = _get_latest_enrollment(student)
        if not latest_enrollment:
            _respond(404, {"status": "failure", "message": "Student enrollment not found"})
            return

        latest_enrollment.whatsapp_response = 1
        student.save(ignore_permissions=True)
        frappe.db.commit()

        batch_id = latest_enrollment.batch or ""
        latest_course_name = latest_enrollment.vertical or ""
        if latest_course_name:
            _respond(200, {
                "course1": latest_course_name,
                "courses_num": 1,
                "batch_id": batch_id,
            })
            return

        school_batch_data = _get_school_course_vertical_names(
            latest_enrollment.school or student.school_id,
            latest_enrollment.grade or student.grade,
        )
        course_names = school_batch_data["course_names"]
        response = {
            f"course{index}": course_name
            for index, course_name in enumerate(course_names, start=1)
        }
        response["courses_num"] = len(course_names)
        response["batch_id"] = batch_id
        _respond(200, response)
        return
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure(
            "student_whatsapp_response",
            {"phone_number": phone_number},
            frappe.get_traceback(),
        )
        frappe.log_error(frappe.get_traceback(), "student_whatsapp_response failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
@log_glific_timeout()
def set_student_course_level(phone_number, course_name):
    try:
        phone, phone_error = _require_valid_phone(phone_number)
        if phone_error:
            _respond(phone_error["code"], phone_error["payload"])
            return
        course_name = _normalize_student_course_name(course_name)

        if course_name not in ALLOWED_STUDENT_COURSE_NAMES:
            _respond(
                400,
                {
                    "status": "failure",
                    "message": "Invalid course_name",
                    "allowed_values": sorted(ALLOWED_STUDENT_COURSE_NAMES),
                },
            )
            return

        student_name = frappe.db.get_value("Student", _phone_filter("phone", phone), "name")
        if not student_name:
            _respond(404, {"status": "failure", "message": "Student not found"})
            return

        student = frappe.get_doc("Student", student_name)
        latest_enrollment = _get_latest_enrollment(student)
        if not latest_enrollment:
            _respond(404, {"status": "failure", "message": "Student enrollment not found"})
            return

        grade_value = latest_enrollment.grade
        if not grade_value:
            _respond(400, {"status": "failure", "message": "Student grade not found"})
            return

        course_level_data, course_level_error = _get_course_vertical_and_level(course_name, grade_value)
        if course_level_error:
            _respond(course_level_error["code"], course_level_error["payload"])
            return

        latest_enrollment.vertical = course_level_data["course_vertical"].name
        latest_enrollment.level = course_level_data["level_label"]
        student.save(ignore_permissions=True)
        _enqueue_glific_contact_sync("Student", student.name)
        frappe.db.commit()

        _respond(
            200,
            {
                "status": "success",
                "student_id": student.name,
                "phone": _phone_for_response(student.phone),
                "course_name": course_level_data["course_vertical"].name2,
                "course_vertical": course_level_data["course_vertical"].name,
                "grade": grade_value,
                "level": course_level_data["level_label"],
            },
        )
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure(
            "set_student_course_level",
            {"phone_number": phone_number, "course_name": course_name},
            frappe.get_traceback(),
        )
        frappe.log_error(frappe.get_traceback(), "set_student_course_level failed")
        _respond(500, {"status": "failure", "message": str(exc)})
