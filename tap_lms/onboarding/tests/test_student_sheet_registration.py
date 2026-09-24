import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


frappe_stub = sys.modules.setdefault("frappe", types.ModuleType("frappe"))
frappe_stub.db = getattr(frappe_stub, "db", MagicMock())
frappe_stub.flags = getattr(frappe_stub, "flags", types.SimpleNamespace())
frappe_stub.session = getattr(
    frappe_stub,
    "session",
    types.SimpleNamespace(user="Administrator"),
)
frappe_stub.ValidationError = getattr(frappe_stub, "ValidationError", Exception)
frappe_stub.DoesNotExistError = getattr(frappe_stub, "DoesNotExistError", Exception)
frappe_stub.logger = getattr(
    frappe_stub,
    "logger",
    MagicMock(return_value=MagicMock()),
)
frappe_stub.get_doc = getattr(frappe_stub, "get_doc", MagicMock())
frappe_stub.throw = getattr(
    frappe_stub,
    "throw",
    MagicMock(side_effect=Exception),
)
frappe_stub.as_json = getattr(frappe_stub, "as_json", MagicMock())
frappe_stub.parse_json = getattr(frappe_stub, "parse_json", MagicMock())
frappe_stub.whitelist = getattr(
    frappe_stub,
    "whitelist",
    lambda *args, **_kwargs: (
        args[0] if args and callable(args[0]) else lambda fn: fn
    ),
)

frappe_utils_stub = sys.modules.setdefault(
    "frappe.utils",
    types.ModuleType("frappe.utils"),
)
frappe_utils_stub.now_datetime = getattr(
    frappe_utils_stub,
    "now_datetime",
    MagicMock(),
)
frappe_utils_stub.getdate = getattr(frappe_utils_stub, "getdate", lambda value: value)
frappe_stub.utils = getattr(frappe_stub, "utils", frappe_utils_stub)

frappe_model_stub = sys.modules.setdefault(
    "frappe.model",
    types.ModuleType("frappe.model"),
)
frappe_document_stub = sys.modules.setdefault(
    "frappe.model.document",
    types.ModuleType("frappe.model.document"),
)
frappe_document_stub.Document = getattr(frappe_document_stub, "Document", object)
frappe_model_stub.document = frappe_document_stub

try:
    import google.auth.transport.requests  # noqa: F401
except Exception:
    google_stub = sys.modules.setdefault("google", types.ModuleType("google"))
    google_auth_stub = types.ModuleType("google.auth")
    google_transport_stub = types.ModuleType("google.auth.transport")
    google_requests_stub = types.ModuleType("google.auth.transport.requests")
    google_requests_stub.AuthorizedSession = MagicMock()
    google_transport_stub.requests = google_requests_stub
    google_auth_stub.transport = google_transport_stub
    google_stub.auth = google_auth_stub
    sys.modules["google.auth"] = google_auth_stub
    sys.modules["google.auth.transport"] = google_transport_stub
    sys.modules["google.auth.transport.requests"] = google_requests_stub

try:
    import google.cloud.storage  # noqa: F401
except Exception:
    google_stub = sys.modules.setdefault("google", types.ModuleType("google"))
    google_cloud_stub = types.ModuleType("google.cloud")
    google_storage_stub = types.ModuleType("google.cloud.storage")
    google_storage_stub.Client = MagicMock()
    google_cloud_stub.storage = google_storage_stub
    google_stub.cloud = google_cloud_stub
    sys.modules["google.cloud"] = google_cloud_stub
    sys.modules["google.cloud.storage"] = google_storage_stub

try:
    import google.oauth2.service_account  # noqa: F401
except Exception:
    google_stub = sys.modules.setdefault("google", types.ModuleType("google"))
    google_oauth2_stub = types.ModuleType("google.oauth2")
    google_service_account_stub = types.ModuleType("google.oauth2.service_account")
    google_service_account_stub.Credentials = MagicMock()
    google_oauth2_stub.service_account = google_service_account_stub
    google_stub.oauth2 = google_oauth2_stub
    sys.modules["google.oauth2"] = google_oauth2_stub
    sys.modules["google.oauth2.service_account"] = google_service_account_stub

from tap_lms.onboarding import student_sheet_registration
from tap_lms.tap_lms.doctype.student_sheet_registration_job import student_sheet_registration_job


class FakeSchool:
    def __init__(self, enrollments):
        self._enrollments = enrollments

    def get(self, fieldname):
        if fieldname == "batch_enrollments":
            return self._enrollments
        return []


class TestStudentSheetRegistrationSchoolLookup(unittest.TestCase):
    def test_school_lookup_uses_glific_when_consent_missing(self):
        with patch.object(
            student_sheet_registration,
            "_get_latest_student_consent",
            return_value=None,
        ), patch.object(
            student_sheet_registration,
            "_get_school_id_from_glific",
            return_value=("SCH-GLIFIC", ""),
        ), patch.object(
            student_sheet_registration,
            "_find_existing_student",
            return_value="ST00000001",
        ) as find_existing_student:
            school_id, error = student_sheet_registration._get_school_id_for_registration(
                "919876543210",
                "Student One",
            )

        self.assertEqual(school_id, "SCH-GLIFIC")
        self.assertEqual(error, "")
        find_existing_student.assert_not_called()

    def test_school_lookup_errors_when_consent_and_glific_school_missing(self):
        with patch.object(
            student_sheet_registration,
            "_get_latest_student_consent",
            return_value=None,
        ), patch.object(
            student_sheet_registration,
            "_get_school_id_from_glific",
            return_value=("", ""),
        ), patch.object(
            student_sheet_registration,
            "_find_existing_student",
            return_value="ST00000001",
        ) as find_existing_student:
            school_id, error = student_sheet_registration._get_school_id_for_registration(
                "919876543210",
                "Student One",
            )

        self.assertEqual(school_id, "")
        self.assertEqual(error, "Student Consent not found and Glific contact school_id not found")
        find_existing_student.assert_not_called()

    def test_school_lookup_returns_glific_error_before_student_fallback(self):
        with patch.object(
            student_sheet_registration,
            "_get_latest_student_consent",
            return_value=None,
        ), patch.object(
            student_sheet_registration,
            "_get_school_id_from_glific",
            return_value=("", "Glific contact school lookup failed: timeout"),
        ), patch.object(
            student_sheet_registration,
            "_find_existing_student",
            return_value="ST00000001",
        ) as find_existing_student:
            school_id, error = student_sheet_registration._get_school_id_for_registration(
                "919876543210",
                "Student One",
            )

        self.assertEqual(school_id, "")
        self.assertEqual(error, "Glific contact school lookup failed: timeout")
        find_existing_student.assert_not_called()

    def test_school_lookup_errors_when_consent_and_glific_contact_missing(self):
        with patch.object(
            student_sheet_registration,
            "_get_latest_student_consent",
            return_value=None,
        ), patch.object(
            student_sheet_registration,
            "_get_school_id_from_glific",
            return_value=("", ""),
        ), patch.object(
            student_sheet_registration,
            "_find_existing_student",
            return_value=None,
        ):
            school_id, error = student_sheet_registration._get_school_id_for_registration(
                "919876543210",
                "Student One",
            )

        self.assertEqual(school_id, "")
        self.assertEqual(error, "Student Consent not found and Glific contact school_id not found")


class TestStudentSheetRegistrationEnrollmentSelection(unittest.TestCase):
    def test_enrollment_selection_uses_latest_doj_before_registration_timestamp(self):
        batch_1 = SimpleNamespace(batch_number="BATCH-1", doj="2026-08-02 00:00:00", idx=1)
        batch_2 = SimpleNamespace(batch_number="BATCH-2", doj="2026-08-17 00:00:00", idx=2)

        with patch.object(
            student_sheet_registration.frappe,
            "get_doc",
            return_value=FakeSchool([batch_1, batch_2]),
        ):
            enrollment, error = student_sheet_registration._get_school_enrollment_for_registration(
                "SCH-001",
                "19/08/2026 10:00:00",
            )
            earlier_enrollment, earlier_error = (
                student_sheet_registration._get_school_enrollment_for_registration(
                    "SCH-001",
                    "16/08/2026 10:00:00",
                )
            )

        self.assertEqual(enrollment.batch_number, "BATCH-2")
        self.assertEqual(error, "")
        self.assertEqual(earlier_enrollment.batch_number, "BATCH-1")
        self.assertEqual(earlier_error, "")

    def test_enrollment_selection_fails_before_first_school_enrollment(self):
        batch_1 = SimpleNamespace(batch_number="BATCH-1", doj="2026-08-02 00:00:00", idx=1)

        with patch.object(
            student_sheet_registration.frappe,
            "get_doc",
            return_value=FakeSchool([batch_1]),
        ):
            enrollment, error = student_sheet_registration._get_school_enrollment_for_registration(
                "SCH-001",
                "2026-08-01 10:00:00",
            )

        self.assertIsNone(enrollment)
        self.assertIn("School Batch Enrollment not found on or before", error)


class TestStudentSheetRegistrationCourseMapping(unittest.TestCase):
    def test_missing_grade_mapping_copies_nearest_lower_grade_and_persists(self):
        enrollment = SimpleNamespace(
            name="SBE-001",
            grades_courses={
                "10": "Financial Literacy",
                "3": "Arts",
                "4": "Arts",
                "5": "Science Lab",
                "6": "Financial Literacy",
                "7": "Coding",
                "8": "Science Lab",
                "9": "Coding",
            },
        )

        with patch.object(
            student_sheet_registration.frappe.db,
            "get_value",
            return_value="CV-FL",
        ), patch.object(
            student_sheet_registration.frappe.db,
            "set_value",
        ) as set_value:
            result = student_sheet_registration._get_course_from_school_enrollment(
                enrollment,
                "12",
            )

        saved_mapping = json.loads(enrollment.grades_courses)
        self.assertEqual(result["course_names"], ["Financial Literacy"])
        self.assertEqual(result["course_vertical"], "CV-FL")
        self.assertEqual(saved_mapping["12"], "Financial Literacy")
        self.assertEqual(list(saved_mapping), ["10", "12", "3", "4", "5", "6", "7", "8", "9"])
        set_value.assert_called_once_with(
            "School Batch Enrollment",
            "SBE-001",
            "grades_courses",
            enrollment.grades_courses,
            update_modified=False,
        )

    def test_missing_grade_mapping_prefers_grade_11_over_grade_10(self):
        enrollment = SimpleNamespace(
            name="SBE-002",
            grades_courses={
                "10": "Financial Literacy",
                "11": "Coding",
            },
        )

        with patch.object(
            student_sheet_registration.frappe.db,
            "get_value",
            return_value="CV-CODING",
        ), patch.object(
            student_sheet_registration.frappe.db,
            "set_value",
        ):
            result = student_sheet_registration._get_course_from_school_enrollment(
                enrollment,
                "12",
            )

        saved_mapping = json.loads(enrollment.grades_courses)
        self.assertEqual(result["course_names"], ["Coding"])
        self.assertEqual(result["course_vertical"], "CV-CODING")
        self.assertEqual(saved_mapping["12"], "Coding")

    def test_multiple_course_options_leave_course_vertical_blank(self):
        enrollment = SimpleNamespace(
            name="SBE-003",
            grades_courses={
                "8": ["Coding", "Science Lab"],
            },
        )

        result = student_sheet_registration._get_course_from_school_enrollment(
            enrollment,
            "8",
        )

        self.assertEqual(result["course_names"], ["Coding", "Science Lab"])
        self.assertEqual(result["course_vertical"], "")


class TestStudentSheetRegistrationGlificContactRow(unittest.TestCase):
    def test_model_falls_back_to_school_batch_enrollment_when_course_blank(self):
        def get_value(doctype, name_or_filters=None, fieldname=None):
            if doctype == "School" and fieldname == "state":
                return "STATE-001"
            if doctype == "State" and fieldname == "state_name":
                return "Delhi"
            if doctype == "School" and fieldname == "model":
                return ""
            if doctype == "Tap Models" and fieldname == "mname":
                return "Batch Enrolment Model"
            if doctype == "Batch" and fieldname == "batch_id":
                return "BT27"
            return ""

        row = {
            "student_name": "Student One",
            "phone": "919876543210",
            "language": "English",
            "school_id": "SC00002099",
            "batch": "BT00000027",
            "course_vertical": "",
            "grade": "12",
            "level": "Level 4",
            "student_id": "ST00000001",
        }

        with patch.object(
            student_sheet_registration.frappe.db,
            "get_value",
            side_effect=get_value,
        ), patch.object(
            student_sheet_registration.frappe.db,
            "sql",
            return_value=[{"model": "MODEL-001"}],
        ):
            contact_row = student_sheet_registration._glific_contact_row(row)

        self.assertEqual(contact_row["model"], "Batch Enrolment Model")
        self.assertEqual(contact_row["course"], "")

    def test_model_uses_selected_enrollment_when_multiple_courses_leave_course_blank(self):
        def get_value(doctype, name_or_filters=None, fieldname=None):
            if doctype == "School" and fieldname == "state":
                return "STATE-001"
            if doctype == "State" and fieldname == "state_name":
                return "Delhi"
            if doctype == "School" and fieldname == "model":
                return ""
            if doctype == "Tap Models" and fieldname == "mname":
                return "Selected Enrolment Model"
            if doctype == "Batch" and fieldname == "batch_id":
                return "BT27"
            return ""

        row = {
            "student_name": "Student One",
            "phone": "919876543210",
            "language": "English",
            "school_id": "SC00002099",
            "batch": "BT00000027",
            "course_vertical": "",
            "course_names": ["Coding", "Science Lab"],
            "model_id": "MODEL-SELECTED",
            "grade": "8",
            "level": "Level 2",
            "student_id": "ST00000001",
        }

        with patch.object(
            student_sheet_registration.frappe.db,
            "get_value",
            side_effect=get_value,
        ), patch.object(
            student_sheet_registration.frappe.db,
            "sql",
        ) as sql:
            contact_row = student_sheet_registration._glific_contact_row(row)

        self.assertEqual(contact_row["model"], "Selected Enrolment Model")
        self.assertEqual(contact_row["course"], "")
        sql.assert_not_called()


class TestStudentSheetRegistrationProcessStatus(unittest.TestCase):
    def test_only_exact_agree_checkmark_is_accepted(self):
        self.assertTrue(student_sheet_registration._contains_agree("Agree ✅"))
        self.assertTrue(student_sheet_registration._contains_agree("  Agree ✅  "))
        self.assertFalse(student_sheet_registration._contains_agree("Agree"))
        self.assertFalse(student_sheet_registration._contains_agree("Disagree"))
        self.assertFalse(student_sheet_registration._contains_agree("agree ✅"))

    def test_done_and_registered_duplicate_statuses_are_complete(self):
        self.assertTrue(student_sheet_registration._status_is_complete("Done"))
        self.assertTrue(student_sheet_registration._status_is_complete(
            student_sheet_registration.DUPLICATE_DONE_MESSAGE
        ))
        self.assertFalse(student_sheet_registration._status_is_complete("Prepared"))

    def test_duplicate_rows_are_complete_process_status(self):
        self.assertEqual(
            student_sheet_registration._process_status_for_row({"message": "Duplicate contact_phone_number"}),
            "complete",
        )

    def test_non_duplicate_errors_are_fail_process_status(self):
        self.assertEqual(
            student_sheet_registration._process_status_for_row({"message": "Invalid grade"}),
            "fail",
        )


class TestStudentSheetRegistrationSheetsWrites(unittest.TestCase):
    def test_status_updates_compact_adjacent_cells_before_writing(self):
        updates = [
            {"spreadsheet_id": "sheet-1", "range": "'Students'!A2", "value": "old"},
            {"spreadsheet_id": "sheet-1", "range": "'Students'!A3", "value": "three"},
            {"spreadsheet_id": "sheet-1", "range": "'Students'!A5", "value": "five"},
            {"spreadsheet_id": "sheet-1", "range": "'Students'!B2", "value": "complete"},
            {"spreadsheet_id": "sheet-1", "range": "'Students'!B3", "value": "fail"},
            {"spreadsheet_id": "sheet-1", "range": "'Students'!A2", "value": "latest"},
        ]

        with patch.object(student_sheet_registration, "_sheets_post") as sheets_post:
            student_sheet_registration._write_status_updates(MagicMock(), updates)

        sheets_post.assert_called_once()
        self.assertEqual(sheets_post.call_args.args[2]["data"], [
            {
                "range": "'Students'!A2:A3",
                "values": [["latest"], ["three"]],
            },
            {"range": "'Students'!A5", "values": [["five"]]},
            {
                "range": "'Students'!B2:B3",
                "values": [["complete"], ["fail"]],
            },
        ])

    def test_sheets_post_retries_429_using_retry_after(self):
        rate_limited = SimpleNamespace(
            ok=False,
            status_code=429,
            headers={"Retry-After": "7"},
            content=b"",
        )
        succeeded = SimpleNamespace(
            ok=True,
            status_code=200,
            headers={},
            content=b'{"updatedCells": 1}',
            json=lambda: {"updatedCells": 1},
        )
        session = MagicMock()
        session.post.side_effect = [rate_limited, succeeded]

        with patch.object(
            student_sheet_registration.random,
            "random",
            return_value=0.25,
        ), patch.object(student_sheet_registration.time_module, "sleep") as sleep:
            result = student_sheet_registration._sheets_post(
                session,
                "https://sheets.googleapis.com/test",
                {"data": []},
            )

        self.assertEqual(result, {"updatedCells": 1})
        self.assertEqual(session.post.call_count, 2)
        sleep.assert_called_once_with(60.25)

    def test_retry_schedule_uses_one_to_sixteen_minute_bases(self):
        response = SimpleNamespace(headers={})
        with patch.object(student_sheet_registration.random, "random", return_value=0.5):
            delays = [
                student_sheet_registration._sheets_retry_delay(response, retry_number)
                for retry_number in range(student_sheet_registration.SHEETS_MAX_RETRIES)
            ]

        self.assertEqual(delays, [60.5, 120.5, 240.5, 480.5, 960.5])

    def test_sheets_post_fails_after_five_minute_scale_waits(self):
        rate_limited = SimpleNamespace(
            ok=False,
            status_code=429,
            headers={},
            content=b"",
            json=lambda: {"error": {"status": "RESOURCE_EXHAUSTED"}},
            text="quota exceeded",
        )
        session = MagicMock()
        session.post.return_value = rate_limited

        with patch.object(
            student_sheet_registration.random,
            "random",
            return_value=0.0,
        ), patch.object(student_sheet_registration.time_module, "sleep") as sleep:
            with self.assertRaises(student_sheet_registration.frappe.ValidationError):
                student_sheet_registration._sheets_post(
                    session,
                    "https://sheets.googleapis.com/test",
                    {"data": []},
                )

        self.assertEqual(session.post.call_count, 6)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [60.0, 120.0, 240.0, 480.0, 960.0],
        )


class TestStudentSheetRegistrationArtifacts(unittest.TestCase):
    def test_done_and_registered_duplicate_rows_are_skipped(self):
        source_sheet = student_sheet_registration.SourceSheet(
            language="English",
            spreadsheet_id="sheet-1",
            sheet_id=0,
            spreadsheet_title="Registrations",
            sheet_title="Students",
            header_map={},
            status_column_index=7,
            process_status_column_index=8,
            rows=[
                {
                    "row_number": 2,
                    "registration_status": "Done",
                    "contact_phone_number": "9876543210",
                },
                {
                    "row_number": 3,
                    "registration_status": student_sheet_registration.DUPLICATE_DONE_MESSAGE,
                    "contact_phone_number": "9123456789",
                },
            ],
        )

        with patch.object(
            student_sheet_registration,
            "_get_sheets_session",
        ), patch.object(
            student_sheet_registration,
            "_read_all_source_sheets",
            return_value=[source_sheet],
        ), patch.object(
            student_sheet_registration,
            "_get_language_id",
            return_value="LANG-EN",
        ), patch.object(
            student_sheet_registration,
            "_prepare_source_row",
        ) as prepare_row, patch.object(
            student_sheet_registration,
            "_write_status_updates",
        ) as write_updates, patch.object(
            student_sheet_registration,
            "_create_and_upload_prepared_workbook",
            return_value="https://example.com/prepared.xlsx",
        ):
            result = student_sheet_registration.prepare_student_sheet_registration(
                log_fn=lambda _message: None
            )

        prepare_row.assert_not_called()
        self.assertEqual(result["summary"]["skipped_done_rows"], 2)
        self.assertEqual(len(write_updates.call_args.args[1]), 2)
        self.assertTrue(all(
            update["value"] == student_sheet_registration.PROCESS_STATUS_COMPLETE
            for update in write_updates.call_args.args[1]
        ))

    def test_preparation_uploads_ready_workbook_and_one_failure_workbook(self):
        source_sheet = student_sheet_registration.SourceSheet(
            language="English",
            spreadsheet_id="sheet-1",
            sheet_id=0,
            spreadsheet_title="Registrations",
            sheet_title="Students",
            header_map={},
            status_column_index=7,
            process_status_column_index=8,
            rows=[
                {"row_number": 2, "registration_status": "", "student_name": "Ready"},
                {"row_number": 3, "registration_status": "", "student_name": "Failed"},
            ],
        )
        ready = {
            "spreadsheet_id": "sheet-1",
            "status_range": "'Students'!G2",
            "process_status_range": "'Students'!H2",
            "prepare_status": "Ready",
            "phone": "919876543210",
            "message": "",
        }
        failed = {
            "spreadsheet_id": "sheet-1",
            "status_range": "'Students'!G3",
            "process_status_range": "'Students'!H3",
            "prepare_status": "Error",
            "phone": "",
            "message": "Invalid grade",
        }

        with patch.object(
            student_sheet_registration,
            "_get_sheets_session",
        ), patch.object(
            student_sheet_registration,
            "_read_all_source_sheets",
            return_value=[source_sheet],
        ), patch.object(
            student_sheet_registration,
            "_get_language_id",
            return_value="LANG-EN",
        ), patch.object(
            student_sheet_registration,
            "_prepare_source_row",
            side_effect=[ready, failed],
        ), patch.object(
            student_sheet_registration,
            "_write_status_updates",
        ), patch.object(
            student_sheet_registration,
            "_create_and_upload_prepared_workbook",
            return_value="https://example.com/prepared.xlsx",
        ) as prepared_workbook, patch.object(
            student_sheet_registration,
            "_create_and_upload_failed_rows_workbook",
            return_value="https://example.com/failures.xlsx",
        ) as failure_workbook:
            result = student_sheet_registration.prepare_student_sheet_registration(
                log_fn=lambda _message: None
            )

        prepared_workbook.assert_called_once_with([ready])
        failure_workbook.assert_called_once_with([failed])
        self.assertEqual(result["summary"]["prepared_file_url"], "https://example.com/prepared.xlsx")
        self.assertEqual(result["summary"]["failed_rows_file_url"], "https://example.com/failures.xlsx")
        self.assertNotIn("duplicate_phone_numbers_file_url", result["summary"])
        self.assertNotIn("other_failures_file_url", result["summary"])

    def test_upload_does_not_reread_source_sheets(self):
        ready = {
            "spreadsheet_id": "sheet-1",
            "sheet_id": 0,
            "sheet_title": "Students",
            "row_number": 2,
            "status_range": "'Students'!G2",
            "process_status_range": "'Students'!H2",
            "prepare_status": "Ready",
            "phone": "919876543210",
            "student_name": "Student One",
        }

        with patch.object(
            student_sheet_registration,
            "_get_sheets_session",
        ), patch.object(
            student_sheet_registration,
            "_read_all_source_sheets",
        ) as read_sheets, patch.object(
            student_sheet_registration,
            "_upsert_student",
            return_value=SimpleNamespace(name="ST00000001"),
        ), patch.object(
            student_sheet_registration,
            "_write_status_updates",
        ), patch.object(
            student_sheet_registration,
            "_create_and_upload_glific_contact_csvs",
            return_value=[],
        ):
            result = student_sheet_registration.upload_prepared_student_sheet_registration(
                [ready],
                log_fn=lambda _message: None,
            )

        read_sheets.assert_not_called()
        self.assertEqual(result["uploaded_rows"], 1)


class TestStudentSheetRegistrationCronCounts(unittest.TestCase):
    def test_duplicate_rows_have_separate_count(self):
        counts = student_sheet_registration_job._cron_log_counts({
            "raw_rows": 5,
            "uploaded_rows": 2,
            "skipped_done_rows": 1,
            "duplicate_rows": 1,
            "failed_rows": 2,
        })

        self.assertEqual(counts["processed_rows"], 5)
        self.assertEqual(counts["successful_rows"], 3)
        self.assertEqual(counts["duplicate_rows"], 1)
        self.assertEqual(counts["failed_rows"], 1)

    def test_summary_counts_persist_not_done_rows_file_url(self):
        with patch.object(student_sheet_registration_job.frappe.db, "set_value") as set_value:
            student_sheet_registration_job._set_summary_counts(
                "SSR-00001",
                {"not_done_rows_file_url": "https://example.com/not-done.csv"},
            )

        updates = set_value.call_args.args[2]
        self.assertEqual(
            updates["not_done_rows_file_url"],
            "https://example.com/not-done.csv",
        )

    def test_cron_log_file_fields_use_explicit_glific_url(self):
        fields = student_sheet_registration_job._cron_log_file_fields({
            "glific_contact_file_url": "https://example.com/glific.csv",
            "not_done_rows_file_url": "https://example.com/not-done.csv",
            "duplicate_phone_numbers_file_url": "https://example.com/duplicates.csv",
            "other_failures_file_url": "https://example.com/other.csv",
        })

        self.assertEqual(fields["glific_contact_file_url"], "https://example.com/glific.csv")
        self.assertEqual(fields["not_done_rows_file_url"], "https://example.com/not-done.csv")
        self.assertEqual(
            fields["duplicate_phone_numbers_file_url"],
            "https://example.com/duplicates.csv",
        )
        self.assertEqual(fields["other_failures_file_url"], "https://example.com/other.csv")

    def test_cron_log_file_fields_fallback_to_glific_contact_files(self):
        fields = student_sheet_registration_job._cron_log_file_fields({
            "glific_contact_files": [
                {"file_path": "https://example.com/glific-1.csv"},
                {"file_path": "https://example.com/glific-2.csv"},
            ],
        })

        self.assertEqual(
            fields["glific_contact_file_url"],
            "https://example.com/glific-1.csv\nhttps://example.com/glific-2.csv",
        )


if __name__ == "__main__":
    unittest.main()
