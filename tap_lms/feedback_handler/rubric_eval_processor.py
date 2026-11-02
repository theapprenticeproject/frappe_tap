from typing import Any, Dict

import frappe
from frappe.utils import now_datetime


def process_rubric_evaluation(message_data: Dict[str, Any]) -> None:
    """Append the rubric rows of an on-demand evaluation. Nothing else on the
    Submission changes and no feedback flow is triggered."""
    submission = frappe.get_doc("Submission", message_data["submission_id"])

    if message_data.get("status") == "Failed":
        submission.rubric_eval_status = "Failed"
        submission.failure_reason = message_data["feedback"].get("error_message")
    else:
        timestamp = now_datetime()
        feedback = message_data["feedback"]
        for rubric in feedback.get("rubric_evaluations", []):
            submission.append("rubric_evaluations", {
                "skill": rubric.get("Skill", ""),
                "grade_value": float(rubric.get("grade_value", 0)),
                "observation": rubric.get("observation", ""),
                "final_grade": feedback.get("final_grade"),
                "submission_validity": feedback.get("submission_validity"),
                "timestamp": timestamp,
                "model_used": message_data.get("model_used"),
                "rubric_eval_job": submission.rubric_eval_job,
            })
        submission.rubric_eval_status = "Completed"

    submission.save(ignore_permissions=True)


def mark_rubric_eval_failed(submission_id: str) -> None:
    frappe.db.set_value("Submission", submission_id, "rubric_eval_status", "Failed")
