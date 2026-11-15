import json
from datetime import datetime
from urllib.parse import urlparse

import frappe

GCS_SETTINGS_NAME = "central-phalanx-297915-Authenticated"


GLIFIC_FEEDBACK_FLOW_ID = "34108"
FEEDBACK_PIPELINE_MAX_RETRIES = 5
FEEDBACK_PIPELINE_RETRY_LOG_TITLE = "Demo Feedback Pipeline Retry"
FEEDBACK_PIPELINE_DLQ_LOG_TITLE = "Demo Feedback Pipeline DLQ - manual replay required"
DEMO_GLIFIC_COMMENT_SOURCE = "demo_submission"
DEMO_GLIFIC_COMMENT_TYPE = "feedback_glific_contact"
DEMO_STUDENT_BY_LANGUAGE = {
    "english": "ST00034562",
    "hindi": "ST00055893",
    "marathi": "ST00388465",
    "punjabi": "ST00223226",
    "kannada": "ST00223225",
}
DEMO_STUDENT_PAYLOAD_DETAILS = {
    "ST00034562": {
        "student_id": "ST00034562",
        "grade": 5,
        "level": 2,
        "language": "English",
    },
    "ST00055893": {
        "student_id": "ST00055893",
        "grade": 5,
        "level": 2,
        "language": "Hindi",
    },
    "ST00388465": {
        "student_id": "ST00388465",
        "grade": 5,
        "level": 2,
        "language": "Marathi",
    },
    "ST00223226": {
        "student_id": "ST00223226",
        "grade": 5,
        "level": 2,
        "language": "Punjabi",
    },
    "ST00223225": {
        "student_id": "ST00223225",
        "grade": 5,
        "level": 2,
        "language": "Kannada",
    },
}


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


def _get_student_payload_details(student_id):
    return DEMO_STUDENT_PAYLOAD_DETAILS.get(
        student_id,
        {
            "student_id": student_id,
            "grade": None,
            "level": None,
            "language": None,
        },
    )


def _normalize_language(language):
    return _normalize_required_text(language, "Language")


def _resolve_demo_student_id(language):
    language_key = language.strip().lower()
    student_id = DEMO_STUDENT_BY_LANGUAGE.get(language_key)
    if not student_id:
        frappe.throw(
            "Unsupported language. Supported languages: "
            + ", ".join(sorted(DEMO_STUDENT_BY_LANGUAGE))
        )

    return student_id


def _create_submission(assignment_id, student_id, payload, language):
    submission_doc = frappe.new_doc("Submission")
    submission_doc.assign_id = assignment_id
    submission_doc.student_id = student_id
    submission_doc.submission_type = payload["submission_type"]
    submission_doc.submission_text = payload["submission_text"]
    submission_doc.submission_url = payload["submission_url"]
    submission_doc.status = "Pending"

    now = datetime.now()
    _set_if_field(submission_doc, "feedback_flow_id", GLIFIC_FEEDBACK_FLOW_ID)
    _set_if_field(submission_doc, "feedback_requested_at", now)
    _set_if_field(submission_doc, "created_at", now)
    _set_if_field(submission_doc, "is_primary", 1)
    _set_if_field(submission_doc, "translation_language", language)

    submission_doc.insert(ignore_permissions=True)
    submission_doc._raw_submission = payload["raw_submission"]
    frappe.logger("submission").info(
        f"Created demo Submission: submission_id={submission_doc.name}, "
        f"assignment_id={assignment_id}, student_id={student_id}, "
        f"language={language}, status={submission_doc.status}"
    )
    return submission_doc


def _add_demo_glific_id_comment(submission_doc, glific_id):
    content = json.dumps(
        {
            "source": DEMO_GLIFIC_COMMENT_SOURCE,
            "type": DEMO_GLIFIC_COMMENT_TYPE,
            "glific_id": glific_id,
            "student_id": submission_doc.student_id,
            "assignment_id": submission_doc.assign_id,
        },
        sort_keys=True,
    )
    comment_doc = frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Comment",
            "reference_doctype": "Submission",
            "reference_name": submission_doc.name,
            "content": content,
        }
    )
    comment_doc.insert(ignore_permissions=True)
    frappe.logger("submission").info(
        f"Stored demo Glific ID comment: submission_id={submission_doc.name}, "
        f"student_id={submission_doc.student_id}, comment_id={comment_doc.name}"
    )
    return comment_doc


def _queue_submission_processing(submission_doc):
    job = frappe.enqueue(
        "tap_lms.imgana.demo_submission.process_submission_async",
        queue="long",
        timeout=600,
        enqueue_after_commit=True,
        submission_id=submission_doc.name,
        raw_submission=getattr(submission_doc, "_raw_submission", None),
    )
    frappe.logger("submission").info(
        f"Registered demo submission processing job after commit: "
        f"submission_id={submission_doc.name}, student_id={submission_doc.student_id}, "
        f"queue=long, job_id={getattr(job, 'id', None)}"
    )


def _build_submission_response(submission_doc, glific_id):
    return {
        "success": True,
        "status": "accepted",
        "message": "Submission received",
        "submission_id": submission_doc.name,
        "assignment_id": submission_doc.assign_id,
        "student_id": submission_doc.student_id,
        "glific_id": glific_id,
    }


@frappe.whitelist(allow_guest=True)
def submit_artwork(assignment_id, glific_id, submission, language):
    """
    Create a demo Submission for the language-specific demo Student and publish
    it to the RabbitMQ feedback pipeline. This endpoint intentionally does not
    inspect enrollment state.
    """
    try:
        assignment_id = _normalize_unicode_surrogates(
            _normalize_required_text(assignment_id, "Assignment ID")
        )
        glific_id = _normalize_required_text(glific_id, "Glific ID")
        language = _normalize_language(language)
        student_id = _resolve_demo_student_id(language)
        raw_submission = str(submission or "")

        frappe.logger("submission").info(
            f"Demo submit_artwork received: assignment_id={assignment_id}, "
            f"student_id={student_id}, glific_id={glific_id}, language={language}, "
            f"submission_chars={len(raw_submission)}, "
            f"submission_is_url={_looks_like_url(raw_submission) if raw_submission.strip() else False}"
        )

        if not frappe.db.exists("Assignment", assignment_id):
            frappe.logger("submission").warning(
                f"Demo submit_artwork rejected: assignment not found, "
                f"assignment_id={assignment_id}, glific_id={glific_id}"
            )
            frappe.local.response.update(
                {
                    "success": False,
                    "status": "not_found",
                    "error_detail": f"Assignment {assignment_id} not found",
                }
            )
            return

        payload = _normalize_submission_payload(submission)
        submission_doc = _create_submission(
            assignment_id,
            student_id,
            payload,
            language,
        )
        _add_demo_glific_id_comment(submission_doc, glific_id)
        _queue_submission_processing(submission_doc)
        frappe.db.commit()
        frappe.logger("submission").info(
            f"Demo submit_artwork committed: submission_id={submission_doc.name}, "
            f"assignment_id={assignment_id}, student_id={student_id}, "
            f"glific_id={glific_id}, language={language}"
        )

        return _build_submission_response(submission_doc, glific_id)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        frappe.logger("submission").warning(
            f"Demo submit_artwork validation error: assignment_id={assignment_id}, "
            f"glific_id={glific_id}, error={str(exc)}"
        )
        frappe.local.response.update(
            {
                "success": False,
                "status": "validation_error",
                "error_detail": str(exc),
            }
        )
        return
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(
            f"submit_artwork failed for assignment_id={assignment_id}, "
            f"glific_id={glific_id}: {type(exc).__name__}: {exc}",
            "Demo Submit Artwork",
        )
        frappe.local.response.update(
            {
                "success": False,
                "status": "internal_error",
                "error_detail": f"{type(exc).__name__}: {exc}",
            }
        )
        return


@frappe.whitelist(allow_guest=True)
def submission_feedback(submission_id):
    """
    API endpoint to get feedback for a demo submission.
    """
    try:
        submission = frappe.get_doc("Submission", submission_id)

        if submission.status == "Completed":
            return {
                "status": submission.status,
                "overall_feedback": submission.overall_feedback,
                "overall_feedback_translated": submission.overall_feedback_translated,
                "audio_feedback_url": submission.audio_feedback_url,
            }

        return {"status": submission.status}

    except frappe.DoesNotExistError:
        return {"error": "Submission not found"}

    except Exception as exc:
        frappe.log_error(
            f"Error checking demo submission status: {str(exc)}",
            "Demo Submission Status Error",
        )
        return {"error": "An error occurred while checking submission status"}


def process_submission_async(submission_id, raw_submission=None, submission_url=None):
    """
    Normalize the raw demo submission, upload URL media to GCS, then publish the
    normalized Submission payload to RabbitMQ for feedback generation.
    """
    try:
        submission_doc = frappe.get_doc("Submission", submission_id)
        raw_submission = (raw_submission or submission_url or "").strip()
        frappe.logger("submission").info(
            f"Demo async processing started: submission_id={submission_id}, "
            f"assignment_id={submission_doc.assign_id}, student_id={submission_doc.student_id}, "
            f"raw_submission_chars={len(raw_submission)}, raw_submission_is_url={_looks_like_url(raw_submission) if raw_submission else False}"
        )

        if raw_submission and _looks_like_url(raw_submission):
            from tap_lms.utils.gcs_client import upload_to_gcs
            from tap_lms.imgana.media_detection import detect_url_media_type

            media_type = detect_url_media_type(raw_submission, default="image")
            frappe.logger("submission").info(
                f"Demo async media detected: submission_id={submission_id}, media_type={media_type}"
            )
            uploaded_url = upload_to_gcs(
                raw_submission,
                submission_doc.name,
                GCS_SETTINGS_NAME,
                media_type=media_type,
            )
            frappe.logger("submission").info(
                f"Demo async media uploaded to GCS: submission_id={submission_id}, "
                f"media_type={media_type}, uploaded_url={uploaded_url}"
            )
            submission_doc.submission_type = media_type
            submission_doc.submission_url = uploaded_url
            submission_doc.submission_text = None
        elif raw_submission:
            submission_doc.submission_type = (
                "emoji" if _contains_only_emoji(raw_submission) else "text"
            )
            frappe.logger("submission").info(
                f"Demo async text normalized: submission_id={submission_id}, "
                f"submission_type={submission_doc.submission_type}"
            )
            submission_doc.submission_text = raw_submission
            submission_doc.submission_url = None
        elif not (submission_doc.submission_text or submission_doc.submission_url):
            frappe.throw("Submission content is missing")

        submission_doc.status = "Processing"
        _set_if_field(submission_doc, "upload_error_log", None)
        submission_doc.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.logger("submission").info(
            f"Demo async processing saved: submission_id={submission_id}, "
            f"status={submission_doc.status}, submission_type={submission_doc.submission_type}"
        )

        enqueue_submission(submission_doc.name)
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(
            f"Error in demo background processing for submission "
            f"{submission_id}: {str(exc)}",
            "Demo process_submission_async",
        )

        try:
            submission_doc = frappe.get_doc("Submission", submission_id)
            submission_doc.status = "Failed"
            _set_if_field(submission_doc, "upload_error_log", frappe.get_traceback()[:5000])
            submission_doc.save(ignore_permissions=True)
            frappe.db.commit()
        except Exception as log_error:
            frappe.log_error(
                f"Failed to update demo submission {submission_id} after "
                f"background error: {str(log_error)}",
                "Demo process_submission_async",
            )

        raise


def enqueue_submission(submission_id, retry_count=0):
    try:
        import pika
        from tap_lms.imgana.submission import get_rabbitmq_settings

        submission_doc = frappe.get_doc("Submission", submission_id)
        frappe.logger("submission").info(
            f"Demo RabbitMQ enqueue started: submission_id={submission_id}, "
            f"student_id={submission_doc.student_id}, "
            f"retry_count={retry_count or 0}"
        )
        payload = {
            "submission_id": submission_doc.name,
            "assign_id": submission_doc.assign_id,
            **_get_student_payload_details(submission_doc.student_id),
            "submission_type": submission_doc.submission_type,
            "submission_text": submission_doc.submission_text,
            "submission_url": submission_doc.submission_url,
            "is_primary": getattr(submission_doc, "is_primary", 1),
            "created_at": str(getattr(submission_doc, "created_at", submission_doc.creation)),
            "source": "demo_submission",
        }

        rabbitmq_config = get_rabbitmq_settings()
        frappe.logger("submission").info(
            f"Demo RabbitMQ settings loaded: submission_id={submission_id}, "
            f"host={rabbitmq_config['host']}, port={rabbitmq_config['port']}, "
            f"virtual_host={rabbitmq_config['virtual_host']}, queue={rabbitmq_config['queue']}"
        )
        credentials = pika.PlainCredentials(
            rabbitmq_config["username"],
            rabbitmq_config["password"],
        )
        parameters = pika.ConnectionParameters(
            rabbitmq_config["host"],
            int(rabbitmq_config["port"]),
            rabbitmq_config["virtual_host"],
            credentials,
        )

        connection = None
        publish_succeeded = False
        try:
            connection = pika.BlockingConnection(parameters)
            frappe.logger("submission").info(
                f"Demo RabbitMQ connected: submission_id={submission_id}, "
                f"queue={rabbitmq_config['queue']}"
            )
            channel = connection.channel()
            channel.confirm_delivery()
            channel.queue_declare(queue=rabbitmq_config["queue"], durable=True)
            frappe.logger("submission").info(
                f"Demo RabbitMQ queue declared: submission_id={submission_id}, "
                f"queue={rabbitmq_config['queue']}"
            )
            channel.basic_publish(
                exchange="",
                routing_key=rabbitmq_config["queue"],
                body=json.dumps(payload, default=str),
                properties=pika.BasicProperties(
                    delivery_mode=2,
                    content_type="application/json",
                ),
                mandatory=True,
            )
            publish_succeeded = True
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception as close_err:
                    frappe.logger("submission").warning(
                        f"RabbitMQ close failed for demo submission {submission_id} "
                        f"(publish_succeeded={publish_succeeded}): {close_err}"
                    )

        if publish_succeeded:
            frappe.logger("submission").info(
                f"Demo RabbitMQ publish succeeded: submission_id={submission_id}, "
                f"queue={rabbitmq_config['queue']}, submission_type={submission_doc.submission_type}"
            )
    except Exception as exc:
        frappe.logger("submission").error(
            f"Failed to enqueue demo submission {submission_id}: {str(exc)}"
        )
        retry_count = (retry_count or 0) + 1
        student_id = ""

        try:
            student_id = frappe.db.get_value("Submission", submission_id, "student_id") or ""
        except Exception:
            student_id = ""

        if retry_count <= FEEDBACK_PIPELINE_MAX_RETRIES:
            frappe.logger("submission").warning(
                f"Demo RabbitMQ publish will retry: submission_id={submission_id}, "
                f"student_id={student_id or 'unknown'}, retry_count={retry_count}, "
                f"error={str(exc)}"
            )
            frappe.log_error(
                title=FEEDBACK_PIPELINE_RETRY_LOG_TITLE,
                message=(
                    f"Demo feedback pipeline transient failure "
                    f"(attempt {retry_count}/{FEEDBACK_PIPELINE_MAX_RETRIES + 1}) "
                    f"for submission {submission_id} "
                    f"(student_id={student_id or 'unknown'}): {exc}"
                ),
            )
            try:
                frappe.enqueue(
                    "tap_lms.imgana.demo_submission.enqueue_submission",
                    queue="default",
                    timeout=120,
                    submission_id=submission_id,
                    retry_count=retry_count,
                )
                frappe.logger("submission").info(
                    f"Demo RabbitMQ retry job queued: submission_id={submission_id}, "
                    f"retry_count={retry_count}, queue=default"
                )
            except Exception as enqueue_err:
                frappe.logger("submission").error(
                    f"Demo RabbitMQ retry enqueue failed: submission_id={submission_id}, "
                    f"retry_count={retry_count}, error={str(enqueue_err)}"
                )
                frappe.log_error(
                    title=FEEDBACK_PIPELINE_DLQ_LOG_TITLE,
                    message=json.dumps(
                        {
                            "reason": "double_fault_enqueue_failed",
                            "submission_id": submission_id,
                            "student_id": student_id,
                            "final_error": str(exc),
                            "enqueue_error": str(enqueue_err),
                            "retries_attempted": retry_count,
                        },
                        indent=2,
                        default=str,
                    ),
                )
        else:
            frappe.logger("submission").error(
                f"Demo RabbitMQ retry budget exhausted: submission_id={submission_id}, "
                f"student_id={student_id or 'unknown'}, retries_attempted={retry_count}, "
                f"error={str(exc)}"
            )
            frappe.log_error(
                title=FEEDBACK_PIPELINE_DLQ_LOG_TITLE,
                message=json.dumps(
                    {
                        "submission_id": submission_id,
                        "student_id": student_id,
                        "final_error": str(exc),
                        "retries_attempted": retry_count,
                    },
                    indent=2,
                    default=str,
                ),
            )
