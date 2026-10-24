# Copyright (c) 2023, Techt4dev and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from tap_lms.onboarding.utils import _ensure_teacher_enrollment


class TestTeacherEnrollment(FrappeTestCase):
    def test_ensure_teacher_enrollment_stores_grade_and_section(self):
        teacher = frappe.new_doc("Teacher")
        school = {"school_id": "SCHOOL-001"}

        _ensure_teacher_enrollment(
            teacher,
            school,
            "BATCH-001",
            grade="7",
            section="B",
        )

        self.assertEqual(len(teacher.enrollment), 1)
        self.assertEqual(teacher.enrollment[0].grade, "7")
        self.assertEqual(teacher.enrollment[0].section, "B")

    def test_ensure_teacher_enrollment_updates_matching_row(self):
        teacher = frappe.new_doc("Teacher")
        school = {"school_id": "SCHOOL-001"}
        _ensure_teacher_enrollment(teacher, school, "BATCH-001", grade="6", section="A")

        _ensure_teacher_enrollment(teacher, school, "BATCH-001", grade="8", section="C")

        self.assertEqual(len(teacher.enrollment), 1)
        self.assertEqual(teacher.enrollment[0].grade, "8")
        self.assertEqual(teacher.enrollment[0].section, "C")

    def test_ensure_teacher_enrollment_allows_na_grade_without_section(self):
        teacher = frappe.new_doc("Teacher")

        _ensure_teacher_enrollment(
            teacher,
            {"school_id": "SCHOOL-001"},
            "BATCH-001",
            grade="NA",
        )

        self.assertEqual(teacher.enrollment[0].grade, "NA")
        self.assertEqual(teacher.enrollment[0].section, "")
