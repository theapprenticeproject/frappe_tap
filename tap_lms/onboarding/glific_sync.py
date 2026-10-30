import frappe

from tap_lms.utils.glific_integration import (
    _set_glific_sync_status,
    create_contact,
    get_contact_by_phone,
    optin_contact,
    register_contact_field,
    update_contact_fields,
)
from tap_lms.onboarding.utils import (
    _get_course_level_label_for_grade,
    _get_language_id_to_name,
    _get_latest_enrollment,
    _get_phone_lookup_variants,
)
from tap_lms.utils.school_utils import get_school_state_model_details


GLIFIC_SYNC_MAX_RETRIES = 3
_SCRATCH_REGISTRATION_FIELDS_BOOTSTRAPPED = False


def _get_contact_by_phone_variants(phone):
    for candidate in _get_phone_lookup_variants(phone):
        contact = get_contact_by_phone(candidate)
        if contact and contact.get("id"):
            return contact
    return None


def _get_glific_language_id(language_name):
    if language_name:
        language_docname = frappe.db.get_value(
            "TAP Language",
            {"language_name": language_name},
            "name",
        )
        if language_docname:
            language_id = frappe.db.get_value(
                "TAP Language",
                language_docname,
                "glific_language_id",
            )
            if language_id:
                return language_id

    return frappe.db.get_value(
        "TAP Language",
        {"language_name": "English"},
        "glific_language_id",
    )


def _get_latest_enrollment_batch_id(doc):
    latest_enrollment = _get_latest_enrollment(doc)
    if latest_enrollment and latest_enrollment.batch:
        return latest_enrollment.batch
    return ""


def _get_latest_enrollment_school_id(doc):
    latest_enrollment = _get_latest_enrollment(doc)
    if latest_enrollment and latest_enrollment.school:
        return latest_enrollment.school
    return getattr(doc, "school_id", None) or ""


def _get_student_course_name(student_doc):
    latest_enrollment = _get_latest_enrollment(student_doc)
    if not latest_enrollment or not latest_enrollment.vertical:
        return ""

    return frappe.db.get_value("Course Verticals", latest_enrollment.vertical, "name2") or ""


def _get_student_grade(student_doc):
    latest_enrollment = _get_latest_enrollment(student_doc)
    return (
        getattr(latest_enrollment, "grade", None)
        if latest_enrollment
        else None
    ) or getattr(student_doc, "grade", None) or ""


def _get_student_level(student_doc):
    level_label, _level_error = _get_course_level_label_for_grade(
        _get_student_grade(student_doc)
    )
    return level_label or ""


def _build_teacher_glific_fields(teacher_doc, school_meta, school_id):
    return {
        "school_id": school_id,
        "state": school_meta["state_name"],
        "model": school_meta["model_name"],
        "buddy_name": (teacher_doc.first_name or "").strip(),
        "batch_id": _get_latest_enrollment_batch_id(teacher_doc),
        "role": teacher_doc.teacher_role or "",
    }


def _build_student_glific_fields(student_doc, school_meta, school_id):
    return {
        "student_id": getattr(student_doc, "name", "") or "",
        "school_id": school_id,
        "school": school_id,
        "state": school_meta["state_name"],
        "model": school_meta["model_name"],
        "buddy_name": (student_doc.name1 or "").strip(),
        "batch_id": _get_latest_enrollment_batch_id(student_doc),
        "grade": _get_student_grade(student_doc),
        "level": _get_student_level(student_doc),
        "course": _get_student_course_name(student_doc),
    }


def enqueue_registration_contact_sync(doctype, docname):
    frappe.enqueue(
        "tap_lms.onboarding.glific_sync.sync_registration_contact_to_glific",
        queue="short",
        timeout=60,
        enqueue_after_commit=True,
        doctype=doctype,
        docname=docname,
        retry_count=0,
    )


def _ensure_glific_registration_fields():
    global _SCRATCH_REGISTRATION_FIELDS_BOOTSTRAPPED

    if _SCRATCH_REGISTRATION_FIELDS_BOOTSTRAPPED:
        return

    required_fields = (
        ("student_id", "student_id"),
        ("school_id", "School ID"),
        ("school", "School"),
        ("state", "State"),
        ("model", "Model"),
        ("buddy_name", "Buddy Name"),
        ("batch_id", "Batch ID"),
        ("role", "Role"),
        ("grade", "Grade"),
        ("level", "Level"),
        ("course", "Course"),
    )

    for shortcode, display_name in required_fields:
        if not register_contact_field(shortcode, display_name):
            raise RuntimeError(f"Failed to register Glific contact field: {shortcode}")

    _SCRATCH_REGISTRATION_FIELDS_BOOTSTRAPPED = True


def sync_registration_contact_to_glific(doctype, docname, retry_count=0):
    try:
        if doctype not in {"Teacher", "Student"}:
            raise ValueError(f"Unsupported doctype for Glific sync: {doctype}")

        _ensure_glific_registration_fields()
        doc = frappe.get_doc(doctype, docname)
        school_id = _get_latest_enrollment_school_id(doc)
        school_meta = get_school_state_model_details(school_id)
        phone = getattr(doc, "phone_number", None) or getattr(doc, "phone", None)

        if not phone:
            raise ValueError(f"{doctype} {docname} has no phone number")

        if doctype == "Teacher":
            contact_name = (doc.first_name or "").strip() or docname
            fields_to_update = _build_teacher_glific_fields(doc, school_meta, school_id)
        else:
            contact_name = (doc.name1 or "").strip() or docname
            fields_to_update = _build_student_glific_fields(doc, school_meta, school_id)

        language_name = _get_language_id_to_name(doc.language)
        language_id = _get_glific_language_id(language_name)
        glific_contact = _get_contact_by_phone_variants(phone)
        created_contact = False

        if glific_contact and glific_contact.get("id"):
            glific_id = str(glific_contact["id"])
            if str(getattr(doc, "glific_id", "") or "").strip() != glific_id:
                frappe.db.set_value(doctype, docname, "glific_id", glific_id)
        else:
            glific_contact = create_contact(
                contact_name,
                phone,
                school_id,
                school_meta["model_name"],
                language_id,
                fields_to_update["batch_id"],
                fields_to_update,
                include_default_school_field=False,
            )
            if not glific_contact or not glific_contact.get("id"):
                raise RuntimeError(f"Failed to create or link Glific contact for {doctype} {docname}")

            glific_id = str(glific_contact["id"])
            frappe.db.set_value(doctype, docname, "glific_id", glific_id)
            created_contact = True

        canonical_phone = (
            str((glific_contact or {}).get("phone") or "").strip()
            or str(phone).strip()
        )
        if not optin_contact(canonical_phone, contact_name):
            raise RuntimeError(
                f"optin_contact returned False for {doctype} {docname} ({canonical_phone})"
            )

        if created_contact:
            _set_glific_sync_status(doctype, docname, "synced")
            frappe.db.commit()
            return

        ok = update_contact_fields(
            glific_id,
            fields_to_update,
            language_id=language_id,
            contact_name=contact_name,
            sync_status_doctype=doctype,
            sync_status_docname=docname,
            existing_fields=(glific_contact or {}).get("fields"),
            fields_to_remove=("school",) if doctype == "Teacher" else None,
        )
        if not ok:
            raise RuntimeError(
                f"update_contact_fields returned False for {doctype} {docname} ({glific_id})"
            )

        frappe.db.commit()
    except Exception as exc:
        retry_count = (retry_count or 0) + 1
        if retry_count <= GLIFIC_SYNC_MAX_RETRIES:
            try:
                from datetime import timedelta
                from frappe.utils.background_jobs import get_queue

                get_queue("short").enqueue_in(
                    timedelta(seconds=min(60, 2 ** retry_count)),
                    "tap_lms.onboarding.glific_sync.sync_registration_contact_to_glific",
                    kwargs={
                        "doctype": doctype,
                        "docname": docname,
                        "retry_count": retry_count,
                    },
                    job_timeout=60,
                )
            except Exception:
                frappe.enqueue(
                    "tap_lms.onboarding.glific_sync.sync_registration_contact_to_glific",
                    queue="short",
                    timeout=60,
                    doctype=doctype,
                    docname=docname,
                    retry_count=retry_count,
                )
            return

        frappe.db.rollback()
        frappe.log_error(
            title="Scratch registration Glific sync failed",
            message=(
                f"doctype={doctype}, docname={docname}, "
                f"retry_count={retry_count}, error={exc}"
            ),
        )
