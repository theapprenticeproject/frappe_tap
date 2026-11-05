##git
import json
import os
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
import frappe

_STOCK_FEEDBACK_CACHE: Optional[Dict[str, Any]] = None


def _load_stock_feedback_data() -> Dict[str, Any]:
    global _STOCK_FEEDBACK_CACHE
    if _STOCK_FEEDBACK_CACHE is not None:
        return _STOCK_FEEDBACK_CACHE

    data_path = os.path.join(os.path.dirname(__file__), "stock_feedback_and_audio.json")
    try:
        with open(data_path, "r", encoding="utf-8") as file:
            _STOCK_FEEDBACK_CACHE = json.load(file)
    except FileNotFoundError:
        frappe.logger().error(f"Stock feedback file not found at {data_path}")
        _STOCK_FEEDBACK_CACHE = {}
    except json.JSONDecodeError as exc:
        frappe.logger().error(f"Invalid JSON in stock feedback file: {exc}")
        _STOCK_FEEDBACK_CACHE = {}

    return _STOCK_FEEDBACK_CACHE


def get_stock_feedback_and_audio(plagiarism_status: str, translation_language: str) -> Dict[str, Optional[str]]:
    status_to_message_type = {
        "Flagged - AI Generated": "invalid_submission_ai",
        "Flagged - Peer Plagiarism": "invalid_submission_peer_plagiarism",
        "Flagged - Self Plagiarism": "invalid_submission_self_plagiarism",
        "Flagged - Exact Match": "invalid_submission_peer_plagiarism",
        "Flagged - Near Duplicate": "invalid_submission_peer_plagiarism",
        "Flagged - Semantic Match": "invalid_submission_peer_plagiarism",
        "Flagged - Reference Plagiarism" : "invalid_submission_reference_plagiarism",
        "system_error": "resend_technical_issue",
        "requirements_mismatch": "invalid_submission_wrong_work",
        "resend_unclear_photo": "resend_technical_issue",
    }
    print(f"Getting stock feedback for plagiarism status: {plagiarism_status} and language: {translation_language}")

    message_type = status_to_message_type.get(plagiarism_status)
    if not message_type or not translation_language:
        return {"translated_feedback": None, "audio_feedback_url": None}

    stock_data = _load_stock_feedback_data()
    language_entries = None

    for language_name, entries in stock_data.items():
        if language_name.lower() == translation_language.lower():
            language_entries = entries
            break

    if not language_entries:
        return {"translated_feedback": None, "audio_feedback_url": None}

    for entry in language_entries:
        if entry.get("message_type") == message_type:
            return {
                "translated_feedback": entry.get("translated_feedback"),
                "audio_feedback_url": entry.get("stock_audio_feedback_url"),
            }

    return {"translated_feedback": None, "audio_feedback_url": None}




class FeedbackProcessor:
    """
    Handles message parsing/validation and all DB updates for feedback processing.
    
    """

    def parse_and_validate(self, body: bytes) -> Tuple[Dict[str, Any], str]:
        try:
            message_data = json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {str(e)}") from e

        submission_id = message_data.get("submission_id")
        if not submission_id:
            raise ValueError("Missing submission_id in message")

        if not message_data.get("feedback"):
            raise ValueError("Missing feedback data in message")

        return message_data, submission_id

    def ensure_submission_exists(self, submission_id: str) -> None:
        # Preserve the existing (odd) behavior: commit and re-check in case another txn created it.
        if frappe.db.exists("Submission", submission_id):
            return

        frappe.logger().error(f"Submission {submission_id} not found. Commiting DB.")
        frappe.db.commit()

        if not frappe.db.exists("Submission", submission_id):
            raise ValueError(f"Submission {submission_id} not found")

    def is_retryable_error(self, error: Exception) -> bool:
        error_str = str(error).lower()

        non_retryable_patterns = [
            "does not exist",
            "not found",
            "invalid",
            "permission denied",
            "duplicate",
            "constraint violation",
            "missing submission_id",
            "missing feedback data",
            "validation error",
        ]

        return not any(pattern in error_str for pattern in non_retryable_patterns)

    def mark_submission_failed(self, submission_id: str, error_message: str) -> None:
        try:
            submission = frappe.get_doc("Submission", submission_id)
            submission.status = "Failed"

            if hasattr(submission, "error_message"):
                submission.error_message = error_message[:500]

            submission.save(ignore_permissions=True)
            frappe.logger().error(f"Marked submission {submission_id} as failed: {error_message}")
        except Exception as e:
            frappe.logger().error(
                f"Error marking submission {submission_id} as failed: {str(e)}"
            )

    def _use_stock(self, key, reason, translation_language):
        frappe.logger().info(f"Using stock audio feedback for submission due to {reason}.")
        fb = get_stock_feedback_and_audio(key, translation_language)
        return fb.get("translated_feedback"), fb.get("audio_feedback_url", "")

    def update_submission(self, message_data: Dict[str, Any]) -> None:
        submission_id = message_data["submission_id"]
        feedback_data = message_data.get("feedback", {})
        plagiarism_data = feedback_data.get("plagiarism_output", {})

        submission = frappe.get_doc("Submission", submission_id)

        # Extract plagiarism data
        is_plagiarized = plagiarism_data.get("is_plagiarized", False)
        is_ai_generated = plagiarism_data.get("is_ai_generated", False)
        match_type = plagiarism_data.get("match_type", "original")
        plagiarism_source = plagiarism_data.get("plagiarism_source", "none")
        similarity_score = plagiarism_data.get("similarity_score", 0.0)
        ai_detection_source = plagiarism_data.get("ai_detection_source")
        ai_confidence = plagiarism_data.get("ai_confidence", 0.0)
        similar_sources = plagiarism_data.get("similar_sources", [])

        # if is_plagiarized:
        #     audio_feedback_url = 
        #     overall_feedback_translated = 


        plagiarism_status = self._determine_plagiarism_status(
            is_plagiarized, is_ai_generated, match_type, plagiarism_source
        )
        result_status = self._determine_result_status(is_plagiarized, is_ai_generated)
        grade = self._extract_grade(feedback_data, submission_id)

        strengths = feedback_data.get("strengths", [])
        strengths_message = "\n".join([f"• {strength}" for strength in strengths])

        areas_for_improvement = feedback_data.get("areas_for_improvement", [])
        areas_for_improvement_message = "\n".join(
            [f"• {area}" for area in areas_for_improvement]
        )

        learning_objectives_feedback = feedback_data.get("learning_objectives_feedback", [])
        learning_objectives_feedback_message = "\n".join(
            [f"• {objective}" for objective in learning_objectives_feedback]
        )

        rubric_evaluations = feedback_data.get("rubric_evaluations", [])
        rubric_evaluations_rows = []
        for rubric in rubric_evaluations:
            skill = rubric.get("Skill")
            if skill == "Content Knowledge" or skill == "Creativity":
                rubric_evaluations_rows.append(
                    {
                        "skill": rubric.get("Skill", ""),
                        "grade_value": float(rubric.get("grade_value", 0)),
                        "observation": rubric.get("observation", ""),
                    }
                )


        # Extract translated feedback and language
        overall_feedback_translated = feedback_data.get("overall_feedback_translated", "")
        translation_language = feedback_data.get("translation_language", "English")
        print("plag status:", plagiarism_status)    
        # Generate audio feedback if translated text and language are provided
        audio_feedback_url = ""

        #using stock feedback and audio for flagged submissions and certain error cases to 
        #ensure appropriate messaging and avoid repeated TTS generation 

        if "Flagged" in plagiarism_status:
            overall_feedback_translated, audio_feedback_url = self._use_stock(plagiarism_status, f"plagiarism status: {plagiarism_status}", translation_language)
        elif "system error" in overall_feedback_translated.lower():
            overall_feedback_translated, audio_feedback_url = self._use_stock("resend_unclear_photo", "system error in feedback", translation_language)
        elif "submission does not match assignment requirements" in overall_feedback_translated.lower():
            overall_feedback_translated, audio_feedback_url = self._use_stock("requirements_mismatch", "requirements mismatch in feedback", translation_language)

        # Audio is now generated upstream by rag_service's RunPod TTS batch
        # pipeline. By the time this message reaches the consumer,
        # audio_feedback_url is already populated (or empty string) by
        # rag_service's release_to_lms_if_ready() before it was queued.
        else:
            audio_feedback_url = (feedback_data.get("audio_feedback_url") or "").strip()
            if audio_feedback_url:
                frappe.logger().info(
                    f"Using upstream TTS audio feedback for submission {submission_id}: {audio_feedback_url}"
                )
            else:
                frappe.logger().info(
                    f"No audio_feedback_url in payload for submission {submission_id}; "
                    f"continuing without audio (upstream TTS was Not Applicable or Failed)."
                )

        if not audio_feedback_url:
            audio_feedback_url = "Null"

        update_data = {
            "status": "Completed",
            "result_status": result_status,
            "completed_at": datetime.now(),
            # Plagiarism fields
            "plagiarism_status": plagiarism_status,
            "is_plagiarized": is_plagiarized,
            "match_type": match_type,
            "plagiarism_source": plagiarism_source,
            "similarity_score": similarity_score * 100,
            "similar_sources": json.dumps(similar_sources),
            # AI detection fields
            "is_ai_generated": is_ai_generated,
            "ai_detection_source": ai_detection_source or "",
            "ai_confidence": ai_confidence * 100,
            # Feedback fields
            "grade": grade,
            # H-3 (2026-06-15): normalize at ingress so the routing-gate check
            # in feedback_consumer_hook.on_feedback_ready can use a single
            # case-sensitive comparison (== "Invalid") instead of defensive
            # title/lower dual-check. AI sources have been observed writing
            # both "Invalid" and "invalid"; canonicalize here.
            "submission_validity": (feedback_data.get("submission_validity") or "").strip().capitalize(),
            "overall_feedback": feedback_data.get("overall_feedback", ""),
            "overall_feedback_translated": overall_feedback_translated,
            "translation_language": translation_language,
            "audio_feedback_url": audio_feedback_url,
            "generated_feedback": json.dumps(feedback_data, indent=2, ensure_ascii=False),
            "learning_objectives_feedback": learning_objectives_feedback_message,
            "strengths": strengths_message,
            "areas_for_improvement": areas_for_improvement_message,
            "encouragement": feedback_data.get("encouragement", ""),
            # Child table field
            "rubric_evaluations": rubric_evaluations_rows,
            "plagiarism_result": message_data.get("plagiarism_score", 0),
        }

        submission.update(update_data)
        submission.save(ignore_permissions=True)

    def _determine_result_status(self, is_plagiarized: bool, is_ai_generated: bool) -> str:
        if is_plagiarized or is_ai_generated:
            return "Success - Flagged"
        return "Success - Original"

    def _determine_plagiarism_status(
        self,
        is_plagiarized: bool,
        is_ai_generated: bool,
        match_type: str,
        plagiarism_source: str,
    ) -> str:
        if is_ai_generated:
            return "Flagged - AI Generated"

        if is_plagiarized:
            if plagiarism_source in ["peer", "peer_collusion"]:
                return "Flagged - Peer Plagiarism"
            if plagiarism_source in ["self_cross_assignment", "self_late_resubmission"]:
                return "Flagged - Self Plagiarism"
            if plagiarism_source == "reference":
                return "Flagged - Reference Plagiarism"

        if not is_plagiarized:
            if match_type == "resubmission_allowed":
                return "Resubmission Allowed"
            return "Original"


    def _extract_grade(self, feedback_data: Dict[str, Any], submission_id: str) -> float:
        grade_recommendation: Any = feedback_data.get("final_grade", "50")

        try:
            if isinstance(grade_recommendation, str):
                grade_clean = "".join(
                    c for c in grade_recommendation if c.isdigit() or c == "."
                )
                grade = float(grade_clean) if grade_clean else 0.0
            else:
                grade = float(grade_recommendation)
        except (ValueError, TypeError):
            grade = 0.0
            frappe.logger().warning(
                f"Could not parse grade '{grade_recommendation}' for submission {submission_id}, using 0.0"
            )

        return grade




# if plagiarism_source == "peer":
#     stock_message = "invalid_submission_peer_plagiarism"
# elif plagiarism_source == "self":
#     stock_message = "invalid_submission_self_plagiarism"
# elif plagiarism_source == "reference":
#     stock_message = "invalid_submission_reference_plagiarism"

# if match_type == "exact_duplicate":
#     pass
# elif match_type == "near_duplicate": semantic_match
#     pass
# elif match_type == "resubmission_allowed":
#     pass
# elif match_type == "original":
#     pass
# elif match_type == "stock_image": ## same as ai generated