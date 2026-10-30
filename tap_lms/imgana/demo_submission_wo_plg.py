import json
import mimetypes
import os
import re
from datetime import datetime
from urllib.parse import unquote, urlparse

import frappe
import requests
from google import genai
from google.genai import types
from google.oauth2 import service_account

from tap_lms.utils.glific_integration import start_contact_flow
from tap_lms.imgana.gcs_client import get_gcs_client, upload_to_gcs
from tap_lms.imgana.media_detection import detect_url_media_type


GEMINI_PROJECT_ID = "central-phalanx-297915"
GEMINI_LOCATION = "global"
GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_THINKING_LEVEL = types.ThinkingLevel.HIGH
GLIFIC_FEEDBACK_FLOW_ID = "34108"
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "Assessment Prompts.txt")
URL_SUBMISSION_TYPES = {"audio", "image", "video"}
GOOGLE_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _normalize_unicode_surrogates(value):
    if not isinstance(value, str):
        return value

    if not any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        return value

    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _normalize_required_text(value, label):
    if value is None:
        frappe.throw(f"{label} is required")

    value = str(value).strip()
    if not value:
        frappe.throw(f"{label} is required")
    return value


def _normalize_submission_payload(submission):
    return {
        "raw_submission": _normalize_required_text(submission, "Submission"),
        "submission_type": None,
        "submission_text": None,
        "submission_url": None,
    }


def _looks_like_url(submission):
    parsed = urlparse(submission.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _contains_only_emoji(submission):
    text = submission.strip()
    if not text:
        return False

    return not any(char.isalnum() for char in text)


def _set_if_field(doc, fieldname, value):
    if doc.meta.has_field(fieldname):
        setattr(doc, fieldname, value)


def _create_submission(assignment_id, glific_id, payload, language):
    submission_doc = frappe.new_doc("Submission")
    submission_doc.assign_id = assignment_id
    submission_doc.student_id = glific_id
    submission_doc.submission_type = payload["submission_type"]
    submission_doc.submission_text = payload["submission_text"]
    submission_doc.submission_url = payload["submission_url"]
    submission_doc.status = "Pending"

    now = datetime.now()
    _set_if_field(submission_doc, "feedback_flow_id", GLIFIC_FEEDBACK_FLOW_ID)
    _set_if_field(submission_doc, "feedback_requested_at", now)
    _set_if_field(submission_doc, "translation_language", language)

    submission_doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return submission_doc


def _build_submission_response(submission_doc):
    return {
        "message": "Submission received",
        "submission_id": submission_doc.name,
        "glific_id": submission_doc.student_id,
        "status": submission_doc.status,
    }


@frappe.whitelist(allow_guest=True)
def submit_artwork(assignment_id, glific_id, submission, language):
    """
    Create a Submission using the Glific contact ID and enqueue Gemini feedback.
    """
    assignment_id = _normalize_unicode_surrogates(
        _normalize_required_text(assignment_id, "Assignment ID")
    )
    glific_id = _normalize_required_text(glific_id, "Glific ID")
    language = _normalize_required_text(language, "Language")

    if not frappe.db.exists("Assignment", assignment_id):
        frappe.throw(f"Assignment {assignment_id} not found")

    payload = _normalize_submission_payload(submission)

    try:
        submission_doc = _create_submission(assignment_id, glific_id, payload, language)
        frappe.enqueue(
            generate_feedback,
            queue="long",
            timeout=900,
            submission_id=submission_doc.name,
            raw_submission=payload["raw_submission"],
            language=language,
        )
        return _build_submission_response(submission_doc)
    except Exception as exc:
        frappe.db.rollback()
        frappe.logger("submission").error(f"Error in submit_artwork: {str(exc)}")
        frappe.throw(f"Failed to submit artwork: {str(exc)}")


def generate_feedback(submission_id, raw_submission=None, language=None):
    """
    Prepare the submission, run Gemini inference, update feedback, then trigger Glific.
    """
    try:
        submission_doc = frappe.get_doc("Submission", submission_id)
        language = (language or submission_doc.get("translation_language") or "English").strip()

        _prepare_submission_for_feedback(submission_doc, raw_submission)

        prompt = _build_prompt(
            assignment_id=submission_doc.assign_id,
            language=language,
            grade_level="Unknown",
        )
        feedback_data = _generate_gemini_feedback(submission_doc, prompt)
        _update_submission_with_feedback(submission_doc.name, feedback_data, language)

        submission_doc = frappe.get_doc("Submission", submission_doc.name)
        _trigger_glific_feedback_flow(submission_doc, feedback_data)
    except Exception:
        frappe.db.rollback()
        _mark_submission_failed(submission_id, frappe.get_traceback())
        raise


def _prepare_submission_for_feedback(submission_doc, raw_submission=None):
    raw_submission = (raw_submission or "").strip()

    if raw_submission and _looks_like_url(raw_submission):
        media_type = detect_url_media_type(raw_submission, default="image")
        submission_doc.submission_url = upload_to_gcs(
            raw_submission,
            submission_doc.name,
            media_type=media_type,
        )
        submission_doc.submission_type = media_type
        submission_doc.submission_text = None
    elif raw_submission:
        submission_doc.submission_type = (
            "emoji" if _contains_only_emoji(raw_submission) else "text"
        )
        submission_doc.submission_text = raw_submission
        submission_doc.submission_url = None
    elif not (submission_doc.submission_text or submission_doc.submission_url):
        frappe.throw("Submission content is missing")

    submission_doc.status = "Processing"
    _set_if_field(submission_doc, "upload_error_log", None)
    submission_doc.save(ignore_permissions=True)
    frappe.db.commit()


def _build_prompt(assignment_id, language, grade_level):
    assignment = frappe.get_doc("Assignment", assignment_id)
    assignment_description = assignment.get("description") or ""

    with open(PROMPT_PATH, "r", encoding="utf-8") as prompt_file:
        prompt = prompt_file.read()

    return (
        prompt.replace("{assignment_description}", assignment_description)
        .replace("{Language}", language or "English")
        .replace("{Grade_Level}", grade_level or "Unknown")
    )


def _get_gemini_settings():
    settings_name = frappe.db.get_value(
        "GCS Settings",
        {"project_id": GEMINI_PROJECT_ID},
        "name",
    )
    if not settings_name:
        frappe.throw(f"GCS Settings not found for project_id '{GEMINI_PROJECT_ID}'")

    return frappe.get_doc("GCS Settings", settings_name)


class _GeminiProvider:
    def __init__(self, settings, model_name, temperature=0.2, max_tokens=4096):
        self.model_name = model_name
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.key_data = self._resolve_service_account_credentials(settings)
        self.location = GEMINI_LOCATION
        self.project_id = settings.project_id
        self.client = self._create_client()

    def _resolve_service_account_credentials(self, settings):
        raw_key = settings.get("credentials_json")
        if isinstance(raw_key, dict):
            return raw_key
        if isinstance(raw_key, str):
            raw_key = raw_key.strip()
            if raw_key:
                try:
                    return json.loads(raw_key)
                except json.JSONDecodeError:
                    return None
        return None

    def _create_client(self):
        if not self.key_data:
            raise ValueError("Gemini service account key JSON is required")

        resolved_project_id = self.project_id or self.key_data.get("project_id")
        credentials = service_account.Credentials.from_service_account_info(
            self.key_data,
            scopes=[GOOGLE_CLOUD_PLATFORM_SCOPE],
        )
        if not resolved_project_id:
            raise ValueError("Project ID is required for Gemini initialization")

        return genai.Client(
            vertexai=True,
            credentials=credentials,
            project=resolved_project_id,
            location=self.location,
        )

    def generate(self, prompt):
        return self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=self._generation_config(),
        )

    def generate_with_media(
        self,
        media_source,
        prompt,
        mime_type=None,
        default_kind="image",
    ):
        media_part = self._build_media_part(
            media_source,
            mime_type=mime_type,
            default_kind=default_kind,
        )
        return self.client.models.generate_content(
            model=self.model_name,
            contents=[media_part, prompt],
            config=self._generation_config(),
        )

    def _generation_config(self):
        return types.GenerateContentConfig(
            temperature=self.temperature,
            max_output_tokens=self.max_tokens,
            response_mime_type="application/json",
            thinking_config=types.ThinkingConfig(
                thinking_level=GEMINI_THINKING_LEVEL,
            ),
        )

    def _build_media_part(self, media_source, mime_type=None, default_kind="image"):
        if isinstance(media_source, dict):
            media_bytes = media_source.get("content")
            resolved_mime_type = mime_type or media_source.get("mime_type")
            if media_bytes is not None:
                return types.Part.from_bytes(
                    data=media_bytes,
                    mime_type=resolved_mime_type
                    or f"{default_kind}/octet-stream",
                )

            media_url = media_source.get("submission_url") or media_source.get("url")
            return types.Part.from_uri(
                file_uri=self._normalize_media_uri(media_url),
                mime_type=resolved_mime_type or _guess_submission_mime_type(
                    media_url,
                    default_kind,
                ),
            )

        if isinstance(media_source, (bytes, bytearray)):
            return types.Part.from_bytes(
                data=bytes(media_source),
                mime_type=mime_type or f"{default_kind}/octet-stream",
            )

        media_url = str(media_source)
        return types.Part.from_uri(
            file_uri=self._normalize_media_uri(media_url),
            mime_type=mime_type or _guess_submission_mime_type(
                media_url,
                default_kind,
            ),
        )

    def _normalize_media_uri(self, media_url):
        if media_url.startswith("https://storage.googleapis.com/"):
            return media_url.replace("https://storage.googleapis.com/", "gs://", 1)
        return media_url


def _create_gemini_provider():
    return _GeminiProvider(
        settings=_get_gemini_settings(),
        model_name=GEMINI_MODEL,
        temperature=0.2,
        max_tokens=4096,
    )


def _generate_gemini_feedback(submission_doc, prompt):
    provider = _create_gemini_provider()

    if (
        submission_doc.submission_type in URL_SUBMISSION_TYPES
        and submission_doc.submission_url
    ):
        media_asset = _download_media_asset(
            submission_doc.submission_url,
            submission_doc.submission_type,
        )
        response = provider.generate_with_media(
            media_asset,
            prompt,
            mime_type=media_asset["mime_type"],
            default_kind=submission_doc.submission_type,
        )
    elif submission_doc.submission_text:
        response = provider.generate(
            f"{prompt}\n\nStudent submission:\n{submission_doc.submission_text}"
        )
    else:
        frappe.throw("Submission content is missing")

    raw_text = response.text
    feedback_data = _parse_feedback_json(raw_text)
    _attach_llm_metadata(feedback_data, response)
    return feedback_data


def _download_media_asset(url, submission_type):
    gcs_object = _parse_gcs_object(url)
    if gcs_object:
        try:
            return _download_gcs_media_asset(
                bucket_name=gcs_object[0],
                object_name=gcs_object[1],
                original_url=url,
                submission_type=submission_type,
            )
        except Exception as exc:
            if urlparse(url).scheme == "gs":
                raise
            frappe.logger("submission").warning(
                f"GCS media download failed for {url}; falling back to HTTP: {exc}"
            )

    return _download_http_media_asset(url, submission_type)


def _download_gcs_media_asset(bucket_name, object_name, original_url, submission_type):
    result = get_gcs_client()
    if result is None:
        frappe.throw("GCS Storage is not enabled. Enable it in GCS Settings.")

    client, _default_bucket_name = result
    blob = client.bucket(bucket_name).blob(object_name)
    try:
        blob.reload()
    except Exception:
        pass

    content = blob.download_as_bytes()
    mime_type = blob.content_type or _guess_submission_mime_type(
        f"gs://{bucket_name}/{object_name}",
        submission_type,
    )
    return {
        "content": content,
        "mime_type": mime_type,
        "url": original_url,
    }


def _download_http_media_asset(url, submission_type):
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    return {
        "content": response.content,
        "mime_type": content_type or _guess_submission_mime_type(url, submission_type),
        "url": url,
    }


def _guess_submission_mime_type(url, submission_type):
    guessed_type = mimetypes.guess_type(url)[0]
    if guessed_type:
        return guessed_type

    defaults = {
        "audio": "audio/mpeg",
        "image": "image/jpeg",
        "video": "video/mp4",
    }
    return defaults.get(submission_type, "image/jpeg")


def _parse_gcs_object(url):
    parsed = urlparse(url)
    if parsed.scheme == "gs" and parsed.netloc and parsed.path:
        return parsed.netloc, unquote(parsed.path.lstrip("/"))

    if parsed.scheme not in {"http", "https"}:
        return None

    host = parsed.netloc
    path = unquote(parsed.path.lstrip("/"))
    if host in {"storage.googleapis.com", "storage.cloud.google.com"} and "/" in path:
        bucket, object_name = path.split("/", 1)
        return bucket, object_name

    suffix = ".storage.googleapis.com"
    if host.endswith(suffix) and path:
        bucket = host[: -len(suffix)]
        return bucket, path

    return None


def _attach_llm_metadata(feedback_data, response):
    try:
        response_data = response.to_dict()
    except Exception:
        return

    candidates = response_data.get("candidates") or [{}]
    feedback_data["_llm_metadata"] = {
        "model_used": GEMINI_MODEL,
        "avg_logprobs": candidates[0].get("avg_logprobs"),
        "cost": _calculate_gemini_cost(response_data),
    }


def _calculate_gemini_cost(response_data):
    metadata = response_data.get("usage_metadata") or {}
    model = response_data.get("model_version", GEMINI_MODEL)

    output_tokens = metadata.get("candidates_token_count", 0)
    input_tokens = metadata.get("total_token_count", 0) - output_tokens

    pricing = {
        "gemini-2.5-pro": {
            "input_std": 1.25,
            "output_std": 10.00,
            "input_long": 2.50,
            "output_long": 15.00,
        },
        "gemini-3.1-pro": {
            "input_std": 2.00,
            "output_std": 12.00,
            "input_long": 4.00,
            "output_long": 18.00,
        },
        "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
        "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
        "gemini-3-flash": {"input": 0.50, "output": 3.00},
        "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    }

    if "2.5-pro" in model or "3.1-pro" in model:
        base_model = "gemini-2.5-pro" if "2.5" in model else "gemini-3.1-pro"
        if input_tokens <= 200000:
            input_rate = pricing[base_model]["input_std"]
            output_rate = pricing[base_model]["output_std"]
        else:
            input_rate = pricing[base_model]["input_long"]
            output_rate = pricing[base_model]["output_long"]
    else:
        if "3.5-flash" in model:
            rates = pricing["gemini-3.5-flash"]
        else:
            rates = pricing.get(model, pricing["gemini-2.5-flash"])
        input_rate = rates["input"]
        output_rate = rates["output"]

    cost = (input_tokens * (input_rate / 1000000)) + (
        output_tokens * (output_rate / 1000000)
    )
    return round(cost, 6)


def _parse_feedback_json(response_text):
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        feedback_data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        feedback_data = json.loads(match.group(0))

    if not isinstance(feedback_data, dict):
        frappe.throw("Gemini feedback must be a JSON object")

    return feedback_data


def _update_submission_with_feedback(submission_id, feedback_data, language):
    submission_doc = frappe.get_doc("Submission", submission_id)
    now = datetime.now()

    _set_if_field(submission_doc, "status", "Completed")
    _set_if_field(submission_doc, "completed_at", now)
    _set_if_field(submission_doc, "feedback_ready_processed_at", now)
    _set_if_field(submission_doc, "translation_language", language)
    _set_if_field(
        submission_doc,
        "generated_feedback",
        json.dumps(feedback_data, indent=2, ensure_ascii=False),
    )

    simple_fields = {
        "overall_feedback": "overall_feedback",
        "overall_feedback_translated": "overall_feedback_translated",
        "encouragement": "encouragement",
        "submission_validity": "submission_validity",
    }
    for source_field, target_field in simple_fields.items():
        if source_field in feedback_data:
            _set_if_field(submission_doc, target_field, feedback_data[source_field] or "")

    list_fields = {
        "strengths": "strengths",
        "areas_for_improvement": "areas_for_improvement",
        "learning_objectives_feedback": "learning_objectives_feedback",
    }
    for source_field, target_field in list_fields.items():
        if source_field in feedback_data:
            _set_if_field(
                submission_doc,
                target_field,
                _format_feedback_value(feedback_data[source_field]),
            )

    if "final_grade" in feedback_data:
        _set_if_field(submission_doc, "grade", _parse_grade(feedback_data["final_grade"]))
    elif "grade" in feedback_data:
        _set_if_field(submission_doc, "grade", _parse_grade(feedback_data["grade"]))

    submission_doc.save(ignore_permissions=True)
    frappe.db.commit()


def _format_feedback_value(value):
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value if item)
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _parse_grade(value):
    try:
        if isinstance(value, str):
            value = "".join(char for char in value if char.isdigit() or char == ".")
        return float(value) if value not in {None, ""} else 0.0
    except (TypeError, ValueError):
        return 0.0


def _trigger_glific_feedback_flow(submission_doc, feedback_data):
    try:
        glific_id = submission_doc.student_id
        if not glific_id:
            frappe.logger("submission").warning(
                f"Submission {submission_doc.name} has no Glific ID; skipping flow"
            )
            return

        default_results = {
            "submission_id": submission_doc.name,
            "feedback": feedback_data.get("overall_feedback", ""),
            "overall_feedback": feedback_data.get("overall_feedback", ""),
            "overall_feedback_translated": feedback_data.get(
                "overall_feedback_translated", ""
            ),
        }
        success = start_contact_flow(
            flow_id=GLIFIC_FEEDBACK_FLOW_ID,
            contact_id=str(glific_id),
            default_results=default_results,
        )

        if success:
            _set_if_field(submission_doc, "feedback_flow_triggered_at", datetime.now())
            submission_doc.save(ignore_permissions=True)
            frappe.db.commit()
        else:
            frappe.logger("submission").warning(
                f"Failed to trigger Glific flow {GLIFIC_FEEDBACK_FLOW_ID} "
                f"for submission {submission_doc.name} and contact {glific_id}"
            )
    except Exception as exc:
        frappe.db.rollback()
        frappe.logger("submission").error(
            f"Error triggering Glific flow {GLIFIC_FEEDBACK_FLOW_ID} "
            f"for submission {submission_doc.name}: {str(exc)}"
        )


def _mark_submission_failed(submission_id, traceback_text):
    try:
        submission_doc = frappe.get_doc("Submission", submission_id)
        submission_doc.status = "Failed"
        _set_if_field(submission_doc, "upload_error_log", traceback_text[:5000])
        submission_doc.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception as exc:
        frappe.logger("submission").error(
            f"Failed to mark submission {submission_id} as failed: {str(exc)}"
        )
