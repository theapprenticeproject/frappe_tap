# Copyright (c) 2023, Techt4dev and contributors
# For license information, please see license.txt

import frappe
import json
import re
from frappe.model.document import Document

logger = frappe.logger("custom_student_webhook", with_more_info=True)
logger.setLevel("INFO")
PHONE_PATTERN = re.compile(r"^\d{10}$")


BLOCKED_BATCH_ID = "BT00000024"
BLOCKED_BATCH_MESSAGE = f"Student is already enrolled in batch {BLOCKED_BATCH_ID} or later; new enrollment blocked"


class Student(Document):
    def autoname(self):
        self.name = _reserve_next_student_name()

    def validate(self):
        if self.is_new():
            return
        saved = set(frappe.db.sql_list(
            """SELECT name FROM "tabStudent Enrollment" WHERE parent = %s AND parenttype = 'Student'""",
            self.name,
        ))
        if any(row.name not in saved for row in self.enrollment) and student_in_blocked_batch(self.name):
            frappe.throw(BLOCKED_BATCH_MESSAGE)


def student_in_blocked_batch(student_name):
    return bool(frappe.db.sql("""
        SELECT 1
        FROM "tabStudent Enrollment" se
        JOIN "tabBatch" b ON b.name = se.batch
        WHERE se.parent = %s AND se.parenttype = 'Student' AND b.batch_id >= %s
        LIMIT 1
    """, (student_name, BLOCKED_BATCH_ID)))


def _reserve_next_student_name():
    frappe.db.sql("""
        INSERT INTO "tabSeries" (name, current)
        VALUES ('ST', 0)
        ON CONFLICT (name) DO NOTHING
    """)
    for _attempt in range(20):
        next_no = frappe.db.sql("""
            WITH max_student AS (
                SELECT COALESCE(
                    max(CASE
                        WHEN name ~ '^ST[0-9]{8}$' THEN substring(name from 3)::integer
                        ELSE 0
                    END),
                    0
                ) AS max_no
                FROM "tabStudent"
            ),
            bumped AS (
                UPDATE "tabSeries" ts
                   SET current = GREATEST(ts.current, ms.max_no) + 1
                  FROM max_student ms
                 WHERE ts.name = 'ST'
             RETURNING ts.current
            )
            SELECT current
            FROM bumped
        """)
        current = int(next_no[0][0]) if next_no else 0
        if current <= 0:
            frappe.throw("Unable to allocate Student ID")

        candidate = f"ST{current:08d}"
        if frappe.db.exists("Student", candidate):
            continue

        # Keep the legacy empty-string series row aligned during rollout so any
        # stale workers still using the old meta do not regress.
        frappe.db.sql("""
            INSERT INTO "tabSeries" (name, current)
            VALUES ('', %s)
            ON CONFLICT (name) DO UPDATE
                  SET current = GREATEST("tabSeries".current, EXCLUDED.current)
        """, (current,))
        return candidate

    frappe.throw("Unable to allocate Student ID after 20 attempts")


def _get_level_for_grade(grade):
    try:
        grade_num = int(str(grade).strip())
    except Exception:
        return ""
    if grade_num <= 3:
        return "Level 0"
    if 4 <= grade_num <= 5:
        return "Level 1"
    if 6 <= grade_num <= 8:
        return "Level 2"
    if 9 <= grade_num <= 10:
        return "Level 3"
    if 11 <= grade_num <= 12:
        return "Level 4"
    return ""


def _get_phone_lookup_variants(phone):
    phone = str(phone or "").strip()
    if len(phone) == 10 and PHONE_PATTERN.fullmatch(phone):
        return [f"91{phone}", phone]
    if len(phone) == 12 and phone.startswith("91") and PHONE_PATTERN.fullmatch(phone[2:]):
        return [phone, phone[2:]]
    return []


def _canonicalize_phone(phone):
    variants = _get_phone_lookup_variants(phone)
    if variants:
        return variants[0]
    raise ValueError("Phone must be exactly 10 digits or 12 digits starting with 91")


def _find_student_for_profile_update(phone):
    variants = _get_phone_lookup_variants(phone)
    if not variants:
        return None

    matches = frappe.get_all(
        "Student",
        filters={"phone": ["in", variants], "profile_id": ""},
        fields=["name"],
        order_by="modified desc",
        limit=1,
    )
    if not matches:
        return None
    return frappe.get_doc("Student", matches[0].name)


@frappe.whitelist()
def register_student():
    """Method to create/register a new student"""
    try:
        logger.info(
            "Entered tap's registration webhook with payload %s", frappe.request.data
        )
        payload = json.loads(frappe.request.data)
        canonical_phone = _canonicalize_phone(payload.get("phone"))
        doc = frappe.new_doc("Student")
        doc.name1 = payload.get("name1")
        doc.phone = canonical_phone
        doc.section = payload.get("section")
        doc.grade = payload.get("grade")
        doc.gender = payload.get("gender")
        doc.level = ""
        doc.rigour = ""
        doc.append(
            "enrollment",
            {
                "batch": payload.get("batch"),
                "grade": payload.get("grade"),
                "level": _get_level_for_grade(payload.get("grade")),
            },
        )
        if payload.get("keyword") and payload.get("keyword") != "":
            try:
                school = frappe.get_last_doc(
                    "School", filters={"keyword": payload.get("keyword")}
                )
                doc.school_id = school.name
            except Exception:
                pass
        doc.insert()
        logger.info("Student with phone %s registered successfully", doc.phone)
        return {"status_code": 200, "message": "Student registered succesfully"}
    except Exception as err:
        raise Exception("Registration webhook : " + str(err))


@frappe.whitelist()
def update_student_profile():
    """Method to update the profile id of a student"""
    try:
        # will have name, phone and profile_id
        payload = json.loads(frappe.request.data)

        logger.info(
            "Entered tap's profile update webhook for profile_id %s",
            payload.get("profile_id"),
        )

        payload_phone = _canonicalize_phone(payload.get("phone"))
        payload_name = payload.get("name1")
        payload_profile_id = payload.get("profile_id")
        payload_batch = payload.get("batch")
        payload_grade = payload.get("grade")

        student = _find_student_for_profile_update(payload_phone)

        if student:
            # update the profile id
            student.phone = payload_phone
            student.profile_id = payload_profile_id
            student.name1 = payload_name
            if payload_grade:
                student.grade = payload_grade
            student.save()
        else:
            # create a new student with the profile, name, phone number and enrollment
            doc = frappe.new_doc("Student")
            doc.name1 = payload_name
            doc.phone = payload_phone
            doc.profile_id = payload_profile_id
            doc.grade = payload_grade
            doc.level = ""
            doc.rigour = ""
            doc.append(
                "enrollment",
                {
                    "batch": payload_batch,
                    "grade": payload_grade,
                    "level": _get_level_for_grade(payload_grade),
                },
            )
            doc.insert()
        logger.info("Updated profile for student with phone %s ", payload_phone)

        return {"status_code": 200, "message": "Profile updated successfully"}
    except Exception as err:
        raise Exception("Profile webhook : " + str(err))
