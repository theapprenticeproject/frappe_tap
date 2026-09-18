import importlib.util
import sys
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch


def _frappe_whitelist(**_kwargs):
    def decorator(fn):
        return fn

    return decorator


def _load_teacher_registration_module():
    module_path = Path(__file__).resolve().parents[1] / "teacher_registration.py"
    module_name = "_teacher_registration_under_test"
    original_modules = {}

    frappe_stub = types.ModuleType("frappe")
    frappe_stub.db = MagicMock()
    frappe_stub.response = {}
    frappe_stub.request = types.SimpleNamespace(data=None)
    frappe_stub.form_dict = {}
    frappe_stub.ValidationError = type("ValidationError", (Exception,), {})
    frappe_stub.whitelist = _frappe_whitelist
    frappe_stub.log_error = MagicMock()
    frappe_stub.get_traceback = MagicMock(return_value="")
    frappe_stub.get_doc = MagicMock()
    frappe_stub.utils = types.SimpleNamespace(
        getdate=lambda value=None: (
            date.fromisoformat(str(value)[:10]) if value else date.today()
        )
    )

    onboarding_utils_stub = types.ModuleType("tap_lms.onboarding.utils")
    for name in (
        "_get_all_school_rows",
        "_get_all_state_district_rows",
        "_enqueue_glific_contact_sync",
        "_ensure_teacher_enrollment",
        "_get_language_id_to_name",
        "_get_language_name_to_id",
        "_get_latest_enrollment",
        "_get_latest_school_batch_id",
        "_get_request_data",
        "_get_school_course_vertical_names",
        "_get_school_row_by_id",
        "_get_school_row_from_input",
        "_normalize_phone",
        "_phone_for_response",
        "_phone_filter",
        "_require_valid_phone",
        "_respond",
        "_validate_api_key_or_respond",
        "_validate_phone",
    ):
        setattr(onboarding_utils_stub, name, MagicMock())

    api_failures_stub = types.ModuleType("tap_lms.utils.api_failures")
    api_failures_stub.log_api_failure = MagicMock()

    glific_timeout_stub = types.ModuleType("tap_lms.utils.glific_timeout")
    glific_timeout_stub.log_glific_timeout = _frappe_whitelist

    stubs = {
        "frappe": frappe_stub,
        "tap_lms.onboarding.utils": onboarding_utils_stub,
        "tap_lms.utils.api_failures": api_failures_stub,
        "tap_lms.utils.glific_timeout": glific_timeout_stub,
    }
    for name, module in stubs.items():
        original_modules[name] = sys.modules.get(name)
        sys.modules[name] = module

    try:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original_module in original_modules.items():
            if original_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original_module


teacher_registration = _load_teacher_registration_module()


class TestTeacherRegistrationRetry(unittest.TestCase):
    def test_check_teacher_exists_returns_latest_current_year_batch(self):
        current_year = date.today().year
        teacher = {
            "enrollment": [
                types.SimpleNamespace(
                    batch="OLD-BATCH",
                    date_joining=f"{current_year - 1}-12-31",
                    idx=3,
                ),
                types.SimpleNamespace(
                    batch="CURRENT-BATCH-1",
                    date_joining=f"{current_year}-01-15",
                    idx=1,
                ),
                types.SimpleNamespace(
                    batch="CURRENT-BATCH-2",
                    date_joining=f"{current_year}-08-20",
                    idx=2,
                ),
            ]
        }

        with patch.object(
            teacher_registration,
            "_get_request_data",
            return_value={"phone": "919999999999"},
        ), patch.object(
            teacher_registration,
            "_normalize_phone",
            return_value="919999999999",
        ), patch.object(
            teacher_registration,
            "_validate_phone",
            return_value=True,
        ), patch.object(
            teacher_registration,
            "_phone_filter",
            return_value={"phone_number": ["in", ["919999999999", "9999999999"]]},
        ), patch.object(
            teacher_registration.frappe.db,
            "exists",
            return_value="TEACHER-1",
        ), patch.object(
            teacher_registration.frappe,
            "get_doc",
            return_value=teacher,
        ), patch.object(
            teacher_registration,
            "_get_latest_enrollment",
            return_value=teacher["enrollment"][2],
        ) as get_latest, patch.object(
            teacher_registration,
            "_respond",
        ) as respond:
            teacher_registration.check_teacher_exists()

        filtered_enrollments = get_latest.call_args.args[0]["enrollment"]
        self.assertEqual(
            [enrollment.batch for enrollment in filtered_enrollments],
            ["CURRENT-BATCH-1", "CURRENT-BATCH-2"],
        )
        respond.assert_called_once_with(
            200,
            {"exists": True, "batch_number": "CURRENT-BATCH-2"},
        )

    def test_check_teacher_exists_returns_empty_batch_without_current_year_enrollment(self):
        teacher = {
            "enrollment": [
                types.SimpleNamespace(
                    batch="OLD-BATCH",
                    date_joining=f"{date.today().year - 1}-12-31",
                    idx=1,
                ),
            ]
        }

        with patch.object(
            teacher_registration,
            "_get_request_data",
            return_value={"phone": "919999999999"},
        ), patch.object(
            teacher_registration,
            "_normalize_phone",
            return_value="919999999999",
        ), patch.object(
            teacher_registration,
            "_validate_phone",
            return_value=True,
        ), patch.object(
            teacher_registration,
            "_phone_filter",
            return_value={"phone_number": ["in", ["919999999999", "9999999999"]]},
        ), patch.object(
            teacher_registration.frappe.db,
            "exists",
            return_value="TEACHER-1",
        ), patch.object(
            teacher_registration.frappe,
            "get_doc",
            return_value=teacher,
        ), patch.object(
            teacher_registration,
            "_get_latest_enrollment",
            return_value=None,
        ) as get_latest, patch.object(
            teacher_registration,
            "_respond",
        ) as respond:
            teacher_registration.check_teacher_exists()

        self.assertEqual(get_latest.call_args.args[0]["enrollment"], [])
        respond.assert_called_once_with(
            200,
            {"exists": True, "batch_number": ""},
        )

    def test_list_state_districts_returns_sorted_location_directory(self):
        locations = [
            {"state": "UTTAR PRADESH", "district": "Agra"},
            {"state": "DELHI", "district": "East"},
            {"state": "DELHI", "district": "West"},
        ]

        with patch.object(
            teacher_registration,
            "_get_request_data",
            return_value={"api_key": "test-key"},
        ), patch.object(
            teacher_registration,
            "_validate_api_key_or_respond",
            return_value=True,
        ), patch.object(
            teacher_registration,
            "_get_all_state_district_rows",
            return_value=locations,
        ), patch.object(
            teacher_registration,
            "_respond",
        ) as respond:
            teacher_registration.list_state_districts()

        respond.assert_called_once_with(
            200,
            {
                "states": ["DELHI", "UTTAR PRADESH"],
                "districts": locations,
            },
        )

    def test_list_school_details_filters_by_state_and_district(self):
        schools = [
            {
                "school_id": "SC00001",
                "school_name": "Test School",
                "city": "Test City",
            },
        ]

        with patch.object(
            teacher_registration,
            "_get_request_data",
            return_value={
                "api_key": "test-key",
                "state_name": "DELHI",
                "district_name": "East",
            },
        ), patch.object(
            teacher_registration,
            "_validate_api_key_or_respond",
            return_value=True,
        ), patch.object(
            teacher_registration,
            "_get_all_school_rows",
            return_value=schools,
        ) as get_schools, patch.object(
            teacher_registration,
            "_respond",
        ) as respond:
            teacher_registration.list_school_details()

        get_schools.assert_called_once_with("DELHI", "East")
        respond.assert_called_once_with(200, {"schools": schools})

    def test_create_teacher_web_retries_transient_db_conflict(self):
        error = Exception("could not serialize access due to concurrent update")
        teacher_registration.frappe.db.rollback.reset_mock()

        with patch.object(
            teacher_registration,
            "_get_request_data",
            return_value={"phone": "919999999999"},
        ), patch.object(
            teacher_registration,
            "_create_teacher_web_once",
            side_effect=[error, None],
        ) as create_once, patch.object(
            teacher_registration,
            "_sleep_before_teacher_write_retry",
        ) as sleep_before_retry, patch.object(
            teacher_registration,
            "_respond",
        ) as respond:
            teacher_registration.create_teacher_web()

        self.assertEqual(create_once.call_count, 2)
        teacher_registration.frappe.db.rollback.assert_called_once()
        sleep_before_retry.assert_called_once_with(0)
        respond.assert_not_called()

    def test_create_teacher_web_returns_500_after_retry_exhaustion(self):
        error = Exception("could not serialize access due to concurrent update")
        teacher_registration.frappe.db.rollback.reset_mock()

        with patch.object(
            teacher_registration,
            "_get_request_data",
            return_value={"phone": "919999999999"},
        ), patch.object(
            teacher_registration,
            "_create_teacher_web_once",
            side_effect=error,
        ) as create_once, patch.object(
            teacher_registration,
            "_sleep_before_teacher_write_retry",
        ) as sleep_before_retry, patch.object(
            teacher_registration,
            "_respond",
        ) as respond:
            teacher_registration.create_teacher_web()

        self.assertEqual(
            create_once.call_count,
            teacher_registration.TEACHER_WRITE_MAX_RETRIES + 1,
        )
        self.assertEqual(
            teacher_registration.frappe.db.rollback.call_count,
            teacher_registration.TEACHER_WRITE_MAX_RETRIES + 1,
        )
        self.assertEqual(
            sleep_before_retry.call_count,
            teacher_registration.TEACHER_WRITE_MAX_RETRIES,
        )
        respond.assert_called_once_with(
            500,
            {
                "status": "failure",
                "message": "could not serialize access due to concurrent update",
            },
        )

    def test_update_teacher_details_uses_teacher_write_retry(self):
        with patch.object(
            teacher_registration,
            "_get_request_data",
            return_value={"phone": "919999999999"},
        ), patch.object(
            teacher_registration,
            "_run_teacher_write_with_retry",
        ) as run_with_retry:
            teacher_registration.update_teacher_details()

        self.assertEqual(run_with_retry.call_args.args[0], "update_teacher_details")

    def test_teacher_whatsapp_response_uses_teacher_write_retry(self):
        with patch.object(
            teacher_registration,
            "_run_teacher_write_with_retry",
        ) as run_with_retry:
            teacher_registration.teacher_whatsapp_response("919999999999")

        self.assertEqual(run_with_retry.call_args.args[0], "teacher_whatsapp_response")


if __name__ == "__main__":
    unittest.main()
