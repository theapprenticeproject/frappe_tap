import json
import re

import frappe
from frappe.utils import getdate, now_datetime


PHONE_PATTERN = re.compile(r"^\d{10}$")


def authenticate_api_key(api_key):
    try:
        api_key_doc = frappe.get_doc("API Key", {"key": api_key, "enabled": 1})
        return api_key_doc.name
    except frappe.DoesNotExistError:
        return None


_SCHOOL_ROW_SELECT = """
    SELECT
        s.name AS school_id,
        s.name1 AS school_name,
        s.state AS state_id,
        COALESCE(st.state_name, s.state, '') AS state,
        COALESCE(d.district_name, s.district, '') AS district,
        COALESCE(c.city_name, s.city, '') AS city
    FROM `tabSchool` s
    LEFT JOIN `tabState` st ON st.name = s.state
    LEFT JOIN `tabDistrict` d ON d.name = s.district
    LEFT JOIN `tabCity` c ON c.name = s.city
"""


def _normalize_phone(phone):
    return str(phone or "").strip()


def _get_phone_lookup_variants(phone):
    phone = _normalize_phone(phone)
    if len(phone) == 10 and PHONE_PATTERN.fullmatch(phone):
        return [f"91{phone}", phone]
    if len(phone) == 12 and phone.startswith("91") and PHONE_PATTERN.fullmatch(phone[2:]):
        return [phone, phone[2:]]
    return []


def _canonicalize_phone(phone):
    variants = _get_phone_lookup_variants(phone)
    return variants[0] if variants else None


def _phone_for_response(phone):
    canonical_phone = _canonicalize_phone(phone)
    if canonical_phone:
        return canonical_phone
    return _normalize_phone(phone)


def _set_status(code):
    frappe.response.http_status_code = code


def _get_current_method_name():
    cmd = (frappe.form_dict or {}).get("cmd")
    if cmd:
        return str(cmd).split(".")[-1]

    path = getattr(getattr(frappe, "request", None), "path", "") or ""
    if path:
        return path.rstrip("/").split("/")[-1].split(".")[-1]

    return "unknown_onboarding_api"


def _log_non_200_response(code, payload):
    if code == 200 or getattr(frappe.flags, "api_failure_logged", False):
        return

    try:
        from tap_lms.utils.api_failures import log_api_failure

        input_payload = _get_request_data()
        log_api_failure(
            _get_current_method_name(),
            input_payload,
            json.dumps(
                {
                    "http_status_code": code,
                    "response_payload": payload,
                },
                default=str,
                ensure_ascii=True,
            ),
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Failed to log non-200 onboarding response: {code}",
        )


def _respond(code, payload):
    _log_non_200_response(code, payload)
    _set_status(code)
    frappe.response.update(payload)
    return


def _get_request_data():
    if getattr(frappe.request, "data", None):
        try:
            return json.loads(frappe.request.data)
        except Exception:
            pass

    if hasattr(frappe.request, "get_json"):
        try:
            data = frappe.request.get_json()
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    return dict(frappe.form_dict or {})


def _validate_api_key_or_respond(api_key):
    if not api_key:
        _respond(400, {"status": "failure", "message": "API key is required"})
        return False

    if not authenticate_api_key(api_key):
        _respond(401, {"status": "failure", "message": "Invalid API key"})
        return False

    return True


def _validate_phone(phone):
    return bool(_canonicalize_phone(phone))


def _require_valid_phone(phone):
    phone = _canonicalize_phone(phone)
    if not phone:
        return None, {
            "code": 400,
            "payload": {
                "status": "failure",
                "message": "Phone must be exactly 10 digits or 12 digits starting with 91",
            },
        }
    return phone, None


def _phone_filter(fieldname, phone):
    variants = _get_phone_lookup_variants(phone)
    if not variants:
        return None
    return {fieldname: ["in", variants]}


def _get_course_level_label_for_grade(grade):
    try:
        grade_num = int(str(grade).strip())
    except Exception:
        return None, {
            "code": 400,
            "payload": {
                "status": "failure",
                "message": f"Invalid grade: {grade}",
            },
        }
    if grade_num <= 3:
        return "Level 0", None
    if 4 <= grade_num <= 5:
        return "Level 1", None
    if 6 <= grade_num <= 8:
        return "Level 2", None
    if 9 <= grade_num <= 10:
        return "Level 3", None
    if 11 <= grade_num <= 12:
        return "Level 4", None

    return None, {
        "code": 400,
        "payload": {
            "status": "failure",
            "message": f"Unsupported grade for course level mapping: {grade}",
        },
    }


def _get_language_name_to_id(language_name):
    if not language_name:
        return None, None

    language_id = frappe.db.get_value(
        "TAP Language",
        {"language_name": language_name},
        "name",
    )
    if not language_id:
        return None, {
            "code": 400,
            "payload": {
                "status": "failure",
                "message": f"Invalid language: {language_name}",
            },
        }
    return language_id, None


def _get_language_id_to_name(language_id):
    if not language_id:
        return ""
    return frappe.db.get_value("TAP Language", language_id, "language_name") or language_id


def _get_school_row_by_id(school_id):
    rows = frappe.db.sql(
        f"""
        {_SCHOOL_ROW_SELECT}
        WHERE s.name = %s
        LIMIT 1
        """,
        (school_id,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _get_all_school_rows(state_name):
    return frappe.db.sql(
        f"""
        {_SCHOOL_ROW_SELECT}
        WHERE UPPER(TRIM(COALESCE(st.state_name, s.state, ''))) = UPPER(%s)
        ORDER BY s.name1 ASC
        """,
        (state_name,),
        as_dict=True,
    )


def _get_school_row_from_input(school_value):
    school_value = (school_value or "").strip()
    if not school_value:
        return None

    rows = frappe.db.sql(
        f"""
        {_SCHOOL_ROW_SELECT}
        WHERE s.name1 = %s
           OR CONCAT(s.name, ' - ', s.name1) = %s
        LIMIT 1
        """,
        (school_value, school_value),
        as_dict=True,
    )
    return rows[0] if rows else None


def _get_latest_child_row(rows, date_attr, empty_date_value):
    def _sort_key(item):
        date_value = getattr(item, date_attr, None)
        if date_value:
            return (1, getdate(date_value), item.idx or 0)
        return (0, getdate(empty_date_value), item.idx or 0)

    rows = list(rows or [])
    if not rows:
        return None
    return max(rows, key=_sort_key)


def _get_latest_school_enrollment(school_id):
    school_id = str(school_id or "").strip()
    if not school_id:
        return None

    school = frappe.get_doc("School", school_id)
    return _get_latest_child_row(
        school.get("batch_enrollments") or [],
        "doj",
        "1900-01-01",
    )


def _get_latest_school_batch_id(school_id):
    latest_enrollment = _get_latest_school_enrollment(school_id)
    if not latest_enrollment:
        return ""

    return latest_enrollment.batch_number or ""


def _get_school_course_vertical_names(school_id, grade):
    if not school_id:
        return {"batch": "", "course_names": [], "vertical": ""}
    grade_key = str(grade or "").strip()
    if not grade_key:
        return {"batch": "", "course_names": [], "vertical": ""}

    latest_enrollment = _get_latest_school_enrollment(school_id)
    if not latest_enrollment:
        return {"batch": "", "course_names": [], "vertical": ""}
    result = {
        "batch": latest_enrollment.batch_number or "",
        "course_names": [],
        "vertical": "",
    }
    grades_courses = latest_enrollment.grades_courses
    if not grades_courses:
        return result

    try:
        grades_courses = frappe.parse_json(grades_courses)
    except Exception:
        return result

    if not isinstance(grades_courses, dict):
        return result

    value = grades_courses.get(grade_key)
    if isinstance(value, str) and value.strip():
        result["course_names"] = [value.strip()]
    elif isinstance(value, list):
        result["course_names"] = [str(item).strip() for item in value if str(item or "").strip()]

    if len(result["course_names"]) == 1:
        result["vertical"] = (
            frappe.db.get_value("Course Verticals", {"name2": result["course_names"][0]}, "name") or ""
        )

    return result


def _ensure_teacher_enrollment(teacher_doc, school_row, batch_id):
    if not school_row or not batch_id:
        return

    for enrollment in teacher_doc.get("enrollment") or []:
        if enrollment.batch == batch_id and enrollment.school == school_row["school_id"]:
            return

    teacher_doc.append(
        "enrollment",
        {
            "batch": batch_id,
            "school": school_row["school_id"],
            "date_joining": now_datetime().date(),
            "whatsapp_response": 0,
        },
    )


def _get_latest_enrollment(doc):
    return _get_latest_child_row(doc.get("enrollment") or [], "date_joining", "1900-01-01")


def _enqueue_glific_contact_sync(doctype, docname):
    from tap_lms.onboarding.glific_sync import enqueue_registration_contact_sync

    enqueue_registration_contact_sync(doctype, docname)
