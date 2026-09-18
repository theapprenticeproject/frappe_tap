import unittest
from time import sleep, time
from urllib.parse import urlsplit, urlunsplit

import requests


url = "https://tap-lms.theapprenticeproject.org/api/method/tap_lms.onboarding.teacher_registration"
Authorization = "token "

url = "http://tap_lms.localhost:8000/api/method/tap_lms.onboarding.student_registration"
Authorization = "token "

API_KEY = ""

PHONE_NUMBER = ""
TEACHER_NAME = "API Teacher"
GENDER = "Female"
ROLE = "Teacher"
LANGUAGE = "English"
SCHOOL_ID = "Test-SC00001"
SCHOOL_NAME = "Test"
STATE_NAME = ""
TIMEOUT = 5
CALL_DELAY_SECONDS = 5
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 1

METHOD_URLS = {
    "list_school_details": "tap_lms.onboarding.teacher_registration.list_school_details",
    "check_teacher_exists": "tap_lms.onboarding.teacher_registration.check_teacher_exists",
    "create_teacher_web": "tap_lms.onboarding.teacher_registration.create_teacher_web",
    "get_teacher_details": "tap_lms.onboarding.teacher_registration.get_teacher_details",
    "update_teacher_details": "tap_lms.onboarding.teacher_registration.update_teacher_details",
    "teacher_whatsapp_response": "tap_lms.onboarding.teacher_registration.teacher_whatsapp_response",
}

TRANSIENT_ERROR_SNIPPETS = (
    "could not serialize access due to concurrent update",
    "current transaction is aborted",
)


class TestTeacherRegistrationAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_url = cls._build_root_url()
        cls.last_call_at = None
        cls.headers = {
            "Authorization": Authorization,
        }
        cls._validate_config()
        cls.create_status_code = None
        cls.create_response = None
        cls.create_response_text = ""
        cls.school_option = SCHOOL_NAME.strip() or SCHOOL_ID.strip()
        cls.school_city = None

    @classmethod
    def _build_root_url(cls):
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @classmethod
    def _validate_config(cls):
        missing = []
        if not url.strip():
            missing.append("url")
        if not Authorization.strip() or Authorization == "••••••":
            missing.append("Authorization")
        if not API_KEY.strip():
            missing.append("API_KEY")
        if not PHONE_NUMBER.strip():
            missing.append("PHONE_NUMBER")
        if not (SCHOOL_ID.strip() or SCHOOL_NAME.strip()):
            missing.append("SCHOOL_ID or SCHOOL_NAME")
        if not STATE_NAME.strip():
            missing.append("STATE_NAME")
        if missing:
            raise unittest.SkipTest(
                f"Set these test configuration values first: {', '.join(missing)}"
            )

    @classmethod
    def _endpoint(cls, method_name):
        return f"{cls.root_url}/api/method/{METHOD_URLS[method_name]}"

    @staticmethod
    def _phone_variants(phone):
        phone = str(phone or "").strip()
        if len(phone) == 10 and phone.isdigit():
            return {phone, f"91{phone}"}
        if len(phone) == 12 and phone.startswith("91") and phone[2:].isdigit():
            return {phone, phone[2:]}
        return {phone}

    def assert_phone_matches(self, actual_phone):
        self.assertIn(str(actual_phone or "").strip(), self._phone_variants(PHONE_NUMBER))

    @classmethod
    def _request(cls, method_name, payload=None):
        if cls.last_call_at is not None:
            elapsed = time() - cls.last_call_at
            if elapsed < CALL_DELAY_SECONDS:
                sleep(CALL_DELAY_SECONDS - elapsed)

        response = requests.post(
            cls._endpoint(method_name),
            data=payload or {},
            headers=cls.headers,
            timeout=TIMEOUT,
        )
        cls.last_call_at = time()
        data = cls._parse_json(response)
        print(
            f"\n[{method_name}] status={response.status_code}\n"
            f"payload={payload or {}}\n"
            f"body={response.text}\n",
            flush=True,
        )
        return response, data

    @staticmethod
    def _parse_json(response):
        try:
            data = response.json()
        except ValueError as exc:
            raise AssertionError(
                f"Expected JSON response. status={response.status_code} body={response.text}"
            ) from exc

        if isinstance(data, dict) and isinstance(data.get("message"), dict):
            return data["message"]
        return data

    @staticmethod
    def _is_transient_failure(response, data):
        if response.status_code != 500:
            return False

        haystacks = [response.text]
        if isinstance(data, dict):
            message = data.get("message")
            if isinstance(message, str):
                haystacks.append(message)
        combined = "\n".join(str(item) for item in haystacks)
        return any(snippet in combined for snippet in TRANSIENT_ERROR_SNIPPETS)

    @classmethod
    def _request_with_retry(cls, method_name, payload=None):
        last_response = None
        last_data = None
        for attempt in range(1, MAX_RETRIES + 1):
            response, data = cls._request(method_name, payload)
            last_response = response
            last_data = data
            if not cls._is_transient_failure(response, data):
                return response, data
            if attempt < MAX_RETRIES:
                sleep(RETRY_DELAY_SECONDS)
        return last_response, last_data

    @classmethod
    def _resolve_school_option(cls):
        if cls.school_option:
            return cls.school_option
        response, data = cls._request_with_retry(
            "list_school_details",
            {"api_key": API_KEY, "state_name": STATE_NAME},
        )
        if response.status_code != 200 or not isinstance(data, dict):
            raise AssertionError(f"Unable to resolve school. body={response.text}")
        schools = data.get("schools") or []
        for school in schools:
            if school.get("school_id") == SCHOOL_ID:
                cls.school_option = school.get("school_name") or SCHOOL_ID
                cls.school_city = school.get("city")
                return cls.school_option
        raise AssertionError(f"School {SCHOOL_ID} not found in list_school_details")

    @classmethod
    def _create_teacher(cls):
        school_option = cls._resolve_school_option()
        response, data = cls._request_with_retry(
            "create_teacher_web",
            {
                "api_key": API_KEY,
                "name": TEACHER_NAME,
                "gender": GENDER,
                "phone": PHONE_NUMBER,
                "role": ROLE,
                "language": LANGUAGE,
                "school": school_option,
            },
        )
        cls.create_status_code = response.status_code
        cls.create_response = data
        cls.create_response_text = response.text
        return response, data

    def test_01_list_school_details(self):
        response, data = self._request_with_retry(
            "list_school_details",
            {"api_key": API_KEY, "state_name": STATE_NAME},
        )

        self.assertEqual(response.status_code, 200, data)
        self.assertIsInstance(data, dict)
        self.assertIn("schools", data)
        self.assertIsInstance(data["schools"], list)
        self.assertTrue(
            all(school.get("state", "").upper() == STATE_NAME.upper() for school in data["schools"]),
            "list_school_details returned a school from another state",
        )
        if SCHOOL_ID:
            matching_school = next(
                (school for school in data["schools"] if school.get("school_id") == SCHOOL_ID),
                None,
            )
            self.assertIsNotNone(matching_school, f"School {SCHOOL_ID} not found")
            self.__class__.school_city = matching_school.get("city")

    def test_02_check_teacher_exists(self):
        response, data = self._request_with_retry(
            "check_teacher_exists",
            {"phone": PHONE_NUMBER},
        )

        self.assertEqual(response.status_code, 200, data)
        self.assertIsInstance(data, dict)
        self.assertIn("exists", data)
        self.assertIsInstance(data["exists"], bool)

    def test_03_create_teacher_web(self):
        response, data = self._create_teacher()

        self.assertIn(
            response.status_code,
            {200, 409},
            f"create_teacher_web unexpected response. body={self.create_response_text}",
        )
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("status"), "success" if response.status_code == 200 else "failure", data)
        if response.status_code == 200:
            self.assertEqual(data["message"], "Teacher created successfully.")
            self.assertIn("teacher_id", data)
        else:
            self.assertEqual(data["message"], "A teacher with this phone number already exists")

    def test_04_get_teacher_details(self):
        if self.create_status_code not in {200, 409, None}:
            self.skipTest(
                f"Skipping because create_teacher_web failed unexpectedly: {self.create_response_text}"
            )

        response, data = self._request_with_retry(
            "get_teacher_details",
            {"phone": PHONE_NUMBER},
        )

        self.assertEqual(response.status_code, 200, data)
        self.assertIsInstance(data, dict)
        self.assertEqual(data["name"], TEACHER_NAME)
        self.assert_phone_matches(data["phone"])
        self.assertIn("state", data)
        self.assertIn("district", data)
        self.assertIn("city", data)
        self.assertIn("school", data)
        self.assertEqual(data["role"], ROLE)
        self.assertEqual(data["language"], LANGUAGE)

    def test_05_update_teacher_details(self):
        if self.create_status_code not in {200, 409, None}:
            self.skipTest(
                f"Skipping because create_teacher_web failed unexpectedly: {self.create_response_text}"
            )

        response, data = self._request_with_retry(
            "update_teacher_details",
            {
                "phone": PHONE_NUMBER,
                "name": TEACHER_NAME,
                "role": ROLE,
                "language": LANGUAGE,
                "school": self._resolve_school_option(),
            },
        )

        self.assertEqual(response.status_code, 200, data)
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("status"), "success", data)
        self.assertEqual(data.get("message"), "Teacher details updated successfully.")

    def test_06_teacher_whatsapp_response(self):
        if self.create_status_code not in {200, 409, None}:
            self.skipTest(
                f"Skipping because create_teacher_web failed unexpectedly: {self.create_response_text}"
            )

        response, data = self._request_with_retry(
            "teacher_whatsapp_response",
            {"phone_number": PHONE_NUMBER},
        )

        self.assertEqual(response.status_code, 200, data)
        self.assertIsInstance(data, dict)
        self.assertIn("/student/", data["student_registration_url"])
        if self.school_city in {"DoE Zone 27", "DoE Zone 28"}:
            self.assertNotIn("student_consent_url", data)
        else:
            self.assertIn("tapschool:", data["student_consent_url"])


if __name__ == "__main__":
    unittest.main()
