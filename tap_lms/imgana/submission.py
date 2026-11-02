import json
from datetime import datetime
from urllib.parse import urlparse

import frappe


# GLIFIC_FEEDBACK_FLOW_ID = "" ## Remove flow id if Feedback flow is not to be triggered by backend. 
GLIFIC_FEEDBACK_FLOW_ID = "41954"
FEEDBACK_PIPELINE_MAX_RETRIES = 5
FEEDBACK_PIPELINE_RETRY_LOG_TITLE = "Feedback Pipeline Retry"
FEEDBACK_PIPELINE_DLQ_LOG_TITLE = "Feedback Pipeline DLQ - manual replay required"


def get_rabbitmq_settings():
    settings = frappe.get_single("RabbitMQ Settings")
    return {
        "host": settings.host,
        "port": int(settings.port),
        "virtual_host": settings.virtual_host,
        "username": settings.username,
        "password": settings.get_password("password"),
        "queue": settings.submission_queue,
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


def _resolve_student_language(student_id):
    language = frappe.db.get_value("Student", student_id, "language")
    return _normalize_required_text(language, "Student language")


def _get_student_payload_details(student_id):
    try:
        student = frappe.get_doc("Student", student_id)
        return {
            "student_id": student.name,
            "grade": student.grade,
            "level": student.level,
            "language": student.language,
        }
    except frappe.DoesNotExistError:
        return {
            "student_id": student_id,
            "grade": None,
            "level": None,
            "language": None,
        }


def _create_submission(assignment_id, student_id, payload, language):
    submission_doc = frappe.new_doc("Submission")
    submission_doc.assign_id = assignment_id
    submission_doc.student_id = student_id
    submission_doc.submission_type = payload["submission_type"]
    submission_doc.submission_text = payload["submission_text"]
    submission_doc.submission_url = payload["submission_url"]
    submission_doc.status = "Pending"
    submission_doc.audio_feedback_url = "Null"

    now = datetime.now()
    _set_if_field(submission_doc, "feedback_flow_id", GLIFIC_FEEDBACK_FLOW_ID)
    _set_if_field(submission_doc, "feedback_requested_at", now)
    _set_if_field(submission_doc, "created_at", now)
    _set_if_field(submission_doc, "is_primary", 1)
    _set_if_field(submission_doc, "translation_language", language)

    submission_doc.insert(ignore_permissions=True)
    submission_doc._raw_submission = payload["raw_submission"]
    frappe.logger("submission").info(
        f"Created Submission: submission_id={submission_doc.name}, "
        f"assignment_id={assignment_id}, student_id={student_id}, "
        f"language={language}, status={submission_doc.status}"
    )
    return submission_doc


def _queue_submission_processing(submission_doc):
    job = frappe.enqueue(
        "tap_lms.imgana.submission.process_submission_async",
        queue="long",
        timeout=600,
        enqueue_after_commit=True,
        submission_id=submission_doc.name,
        raw_submission=getattr(submission_doc, "_raw_submission", None),
    )
    frappe.logger("submission").info(
        f"Registered submission processing job after commit: "
        f"submission_id={submission_doc.name}, student_id={submission_doc.student_id}, "
        f"queue=long, job_id={getattr(job, 'id', None)}"
    )


def _build_submission_response(submission_doc):
    return {
        "success": True,
        "status": "accepted",
        "message": "Submission received",
        "submission_id": submission_doc.name,
        "assignment_id": submission_doc.assign_id,
        "student_id": submission_doc.student_id,
    }


@frappe.whitelist(allow_guest=True)
def submit_artwork(assignment_id, student_id, submission):
    """
    Create a regular student Submission and publish it to the RabbitMQ feedback
    pipeline. Language is resolved from the Student record.
    """
    try:
        assignment_id = _normalize_unicode_surrogates(
            _normalize_required_text(assignment_id, "Assignment ID")
        )
        student_id = _normalize_unicode_surrogates(
            _normalize_required_text(student_id, "Student ID")
        )
        raw_submission = str(submission or "")

        frappe.logger("submission").info(
            f"submit_artwork received: assignment_id={assignment_id}, "
            f"student_id={student_id}, submission_chars={len(raw_submission)}, "
            f"submission_is_url={_looks_like_url(raw_submission) if raw_submission.strip() else False}"
        )

        if not frappe.db.exists("Assignment", assignment_id):
            frappe.logger("submission").warning(
                f"submit_artwork rejected: assignment not found, "
                f"assignment_id={assignment_id}, student_id={student_id}"
            )
            frappe.local.response.update(
                {
                    "success": False,
                    "status": "not_found",
                    "error_detail": f"Assignment {assignment_id} not found",
                }
            )
            return

        if not frappe.db.exists("Student", student_id):
            frappe.logger("submission").warning(
                f"submit_artwork rejected: student not found, "
                f"assignment_id={assignment_id}, student_id={student_id}"
            )
            frappe.local.response.update(
                {
                    "success": False,
                    "status": "not_found",
                    "error_detail": f"Student {student_id} not found",
                }
            )
            return

        language = _resolve_student_language(student_id)
        payload = _normalize_submission_payload(submission)
        submission_doc = _create_submission(
            assignment_id,
            student_id,
            payload,
            language,
        )
        _queue_submission_processing(submission_doc)
        frappe.db.commit()
        frappe.logger("submission").info(
            f"submit_artwork committed: submission_id={submission_doc.name}, "
            f"assignment_id={assignment_id}, student_id={student_id}, "
            f"language={language}"
        )

        return _build_submission_response(submission_doc)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        frappe.logger("submission").warning(
            f"submit_artwork validation error: assignment_id={assignment_id}, "
            f"student_id={student_id}, error={str(exc)}"
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
            f"student_id={student_id}: {type(exc).__name__}: {exc}",
            "Submit Artwork",
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
def submit_artwork_audio(submission_id, audio):
    """Queue an audio explanation URL for upload to GCS and save it."""
    try:
        if not _looks_like_url(audio):
            frappe.throw("Audio must be a valid HTTP or HTTPS URL")

        if not frappe.db.exists("Submission", submission_id):
            frappe.local.response.update(
                {
                    "success": False,
                    "status": "submission_not_found",
                    "error_detail": f"Submission {submission_id} not found",
                }
            )
            return

        job = frappe.enqueue(
            "tap_lms.imgana.submission.process_artwork_audio_async",
            queue="long",
            timeout=600,
            enqueue_after_commit=True,
            submission_id=submission_id,
            audio=audio,
        )
        frappe.db.commit()
        frappe.logger("submission").info(
            f"Artwork audio upload queued: submission_id={submission_id}, "
            f"queue=long, job_id={getattr(job, 'id', None)}"
        )

        return {
            "success": True,
            "status": "accepted",
            "message": "Audio explanation received",
            "submission_id": submission_id,
        }
    except frappe.ValidationError as exc:
        frappe.db.rollback()
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
            f"submit_artwork_audio failed for submission_id={submission_id}: "
            f"{type(exc).__name__}: {exc}",
            "Submit Artwork Audio",
        )
        frappe.local.response.update(
            {
                "success": False,
                "status": "internal_error",
                "error_detail": f"{type(exc).__name__}: {exc}",
            }
        )
        return


def process_artwork_audio_async(submission_id, audio):
    """Upload an audio explanation URL to GCS and save its resulting URL."""
    try:
        from tap_lms.imgana.gcs_client import upload_to_gcs

        uploaded_url = upload_to_gcs(audio, submission_id, media_type="audio")
        frappe.db.set_value(
            "Submission",
            submission_id,
            "audio_explanation",
            uploaded_url,
            update_modified=False,
        )
        frappe.db.commit()
        frappe.logger("submission").info(
            f"Artwork audio saved: submission_id={submission_id}, "
            f"audio_explanation={uploaded_url}"
        )
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(
            f"Artwork audio processing failed for submission_id={submission_id}: "
            f"{type(exc).__name__}: {exc}",
            "Process Artwork Audio",
        )
        raise


@frappe.whitelist(allow_guest=True)
def submission_feedback(submission_id):
    """
    API endpoint to get feedback for a regular submission.
    """
    try:
        submission = frappe.get_doc("Submission", submission_id)

        if submission.status == "Completed":
            return {
                "status": submission.status,
                "overall_feedback": submission.overall_feedback,
                "overall_feedback_translated": submission.overall_feedback_translated,
                "audio_feedback_url": submission.audio_feedback_url,
                "submission_validity": submission.submission_validity,
            }

        return {"status": submission.status}

    except frappe.DoesNotExistError:
        return {"error": "Submission not found"}

    except Exception as exc:
        frappe.log_error(
            f"Error checking submission status: {str(exc)}",
            "Submission Status Error",
        )
        return {"error": "An error occurred while checking submission status"}


def process_submission_async(submission_id, raw_submission=None, submission_url=None):
    """
    Normalize the raw submission, upload URL media to GCS, then publish the
    normalized Submission payload to RabbitMQ for feedback generation.
    """
    try:
        submission_doc = frappe.get_doc("Submission", submission_id)
        raw_submission = (raw_submission or submission_url or "").strip()
        frappe.logger("submission").info(
            f"Async processing started: submission_id={submission_id}, "
            f"assignment_id={submission_doc.assign_id}, student_id={submission_doc.student_id}, "
            f"raw_submission_chars={len(raw_submission)}, raw_submission_is_url={_looks_like_url(raw_submission) if raw_submission else False}"
        )

        if raw_submission and _looks_like_url(raw_submission):
            from tap_lms.imgana.gcs_client import upload_to_gcs
            from tap_lms.imgana.media_detection import detect_url_media_type

            media_type = detect_url_media_type(raw_submission, default="image")
            frappe.logger("submission").info(
                f"Async media detected: submission_id={submission_id}, media_type={media_type}"
            )
            uploaded_url = upload_to_gcs(
                raw_submission,
                submission_doc.name,
                media_type=media_type,
            )
            frappe.logger("submission").info(
                f"Async media uploaded to GCS: submission_id={submission_id}, "
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
                f"Async text normalized: submission_id={submission_id}, "
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
            f"Async processing saved: submission_id={submission_id}, "
            f"status={submission_doc.status}, submission_type={submission_doc.submission_type}"
        )

        enqueue_submission(submission_doc.name)
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(
            f"Error in background processing for submission "
            f"{submission_id}: {str(exc)}",
            "process_submission_async",
        )

        try:
            submission_doc = frappe.get_doc("Submission", submission_id)
            submission_doc.status = "Failed"
            _set_if_field(submission_doc, "upload_error_log", frappe.get_traceback()[:5000])
            submission_doc.save(ignore_permissions=True)
            frappe.db.commit()
        except Exception as log_error:
            frappe.log_error(
                f"Failed to update submission {submission_id} after "
                f"background error: {str(log_error)}",
                "process_submission_async",
            )

        raise


def enqueue_submission(submission_id, retry_count=0):
    try:
        import pika

        submission_doc = frappe.get_doc("Submission", submission_id)
        frappe.logger("submission").info(
            f"RabbitMQ enqueue started: submission_id={submission_id}, "
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
        }

        rabbitmq_config = get_rabbitmq_settings()
        frappe.logger("submission").info(
            f"RabbitMQ settings loaded: submission_id={submission_id}, "
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
                f"RabbitMQ connected: submission_id={submission_id}, "
                f"queue={rabbitmq_config['queue']}"
            )
            channel = connection.channel()
            channel.confirm_delivery()
            channel.queue_declare(queue=rabbitmq_config["queue"], durable=True)
            frappe.logger("submission").info(
                f"RabbitMQ queue declared: submission_id={submission_id}, "
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
                        f"RabbitMQ close failed for submission {submission_id} "
                        f"(publish_succeeded={publish_succeeded}): {close_err}"
                    )

        if publish_succeeded:
            frappe.logger("submission").info(
                f"RabbitMQ publish succeeded: submission_id={submission_id}, "
                f"queue={rabbitmq_config['queue']}, submission_type={submission_doc.submission_type}"
            )
    except Exception as exc:
        frappe.logger("submission").error(
            f"Failed to enqueue submission {submission_id}: {str(exc)}"
        )
        retry_count = (retry_count or 0) + 1
        student_id = ""

        try:
            student_id = frappe.db.get_value("Submission", submission_id, "student_id") or ""
        except Exception:
            student_id = ""

        if retry_count <= FEEDBACK_PIPELINE_MAX_RETRIES:
            frappe.logger("submission").warning(
                f"RabbitMQ publish will retry: submission_id={submission_id}, "
                f"student_id={student_id or 'unknown'}, retry_count={retry_count}, "
                f"error={str(exc)}"
            )
            frappe.log_error(
                title=FEEDBACK_PIPELINE_RETRY_LOG_TITLE,
                message=(
                    f"Feedback pipeline transient failure "
                    f"(attempt {retry_count}/{FEEDBACK_PIPELINE_MAX_RETRIES + 1}) "
                    f"for submission {submission_id} "
                    f"(student_id={student_id or 'unknown'}): {exc}"
                ),
            )
            try:
                frappe.enqueue(
                    "tap_lms.imgana.submission.enqueue_submission",
                    queue="default",
                    timeout=120,
                    submission_id=submission_id,
                    retry_count=retry_count,
                )
                frappe.logger("submission").info(
                    f"RabbitMQ retry job queued: submission_id={submission_id}, "
                    f"retry_count={retry_count}, queue=default"
                )
            except Exception as enqueue_err:
                frappe.logger("submission").error(
                    f"RabbitMQ retry enqueue failed: submission_id={submission_id}, "
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
                f"RabbitMQ retry budget exhausted: submission_id={submission_id}, "
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


@frappe.whitelist(allow_guest=True)
def get_assignment_context(assignment_id, student_id=None):
    """Get complete assignment context for RAG service"""
    try:
        assignment_id = _normalize_unicode_surrogates(assignment_id)
        assignment = frappe.get_doc("Assignment", assignment_id)
        images = []
        for row in assignment.get("reference_images") or []:
            file_url = row.get("image")
            if not file_url:
                continue

            try:
                file_doc = frappe.get_doc("File", {"file_url": file_url})
                file_path = file_doc.get_full_path()
                with open(file_path, "rb") as image_file:
                    content = base64.b64encode(image_file.read()).decode("utf-8")

                content_type = mimetypes.guess_type(file_doc.file_name or file_url)[0] or "image/jpeg"
                images.append({
                    "name": row.get("image_name") or file_doc.file_name,
                    "content_type": content_type,
                    "content": content,
                })
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Assignment Context Image Error - {assignment_id}",
                )

        rubrics = {}
        for grade in assignment.get("rubric_grades") or []:
            rubric_key = grade.get("rubric_name") or grade.get("skill_name") or "General"
            rubrics.setdefault(rubric_key, []).append({
                "grade_value": grade.get("grade_value"),
                "grade_name": grade.get("grade_name"),
                "grade_description": grade.get("grade_description"),
                "skill_name": grade.get("skill_name"),
            })

        learning_objectives = []
        for objective_row in assignment.get("learning_objectives") or []:
            objective_name = objective_row.get("learning_objective")
            if not objective_name:
                continue

            learning_objectives.append({
                "objective": objective_name,
                "description": frappe.db.get_value(
                    "Learning Objective",
                    objective_name,
                    "description",
                ),
            })

        submission_rules = []
        for rule in assignment.get("submission_rules") or []:
            submission_rules.append({
                "submission_title": rule.get("submission_title"),
                "allowed_submission_types": [
                    item.strip()
                    for item in (rule.get("allowed_submission_types") or "").split(",")
                    if item.strip()
                ],
                "guided_text": rule.get("guided_text"),
                "unguided_text": rule.get("unguided_text"),
                "valid_criteria": rule.get("valid_criteria"),
                "invalid_criteria": rule.get("invalid_criteria"),
            })

        context = {
            "assignment": {
                "name": assignment.get("assignment_name"),
                "program_name": assignment.get("program_name"),
                "description": assignment.get("description"),
                "assignment_type": assignment.get("assignment_type"),
                "activity_type": assignment.get("activity_type"),
                "course_vertical": assignment.get("subject"),
                "difficulty_tier": assignment.get("difficulty_tier"),
                "submission_guidelines": assignment.get("submission_guidelines"),
                "submission_rules": submission_rules,
                "reference_images": images,
                "max_score": assignment.get("max_score"),
                "rubrics": rubrics,
            },
            "learning_objectives": learning_objectives,
        }

        # Add custom feedback prompt if enabled
        if assignment.enable_auto_feedback and assignment.feedback_prompt:
            context["feedback_prompt"] = assignment.feedback_prompt
            
        return context
        
    except Exception as e:
        frappe.log_error(
            f"Error getting assignment context: {str(e)}",
            "RAG Context Error"
        )
        return None
