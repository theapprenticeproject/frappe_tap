"""
Scheduled delivery of student feedback through Glific.

The RabbitMQ FeedbackConsumer saves AI feedback as it arrives. When "Enable
Batch Feedback Delivery" is on in Feedback Delivery Settings, the consumer no
longer starts the Glific feedback flow; the scheduled dispatch wrappers run it
at 3pm, 4:30pm, and 9pm IST (see hooks.py) for every submission that is ready.

dispatch() creates a Feedback Delivery Run (which also stops two runs from
overlapping) and enqueues the first page. process_chunk() delivers one page,
then enqueues the next, so only one page job exists at a time.

Per submission, _deliver() claims it, finds the Glific contact id and starts
the flow. The contact id comes from Student.glific_id; if that is empty it is
looked up by phone and saved to the Student when the contact's student_id
field matches. Missing data marks the submission "skipped:<reason>" (not
retried). Glific failures are retried at the next window, then the submission
is "parked". To re-queue either, clear feedback_delivery_status and
feedback_delivery_attempts.
"""
import time

import frappe
from frappe.utils import add_to_date, now_datetime

from tap_lms.glific_integration import (
    _parse_glific_fields_blob,
    get_contact_by_phone,
    start_contact_flow,
)

RUN_DOCTYPE = "Feedback Delivery Run"
DLQ_LOG_TITLE = "Feedback Delivery DLQ - manual replay required"
ERROR_LOG_TITLE = "Feedback Delivery"

# Submissions that still need a feedback flow.
_ELIGIBLE = """
    status IN ('Completed', 'Failed')
    AND COALESCE(feedback_flow_id, '') != ''
    AND feedback_flow_triggered_at IS NULL
    AND COALESCE(feedback_delivery_status, '') = ''
    AND COALESCE(feedback_delivery_attempts, 0) < %(max_attempts)s
    AND (feedback_claimed_at IS NULL OR feedback_claimed_at < %(lease_cutoff)s)
    AND COALESCE(feedback_requested_at, creation) >= %(oldest)s
"""


class _Skip(Exception):
    """Submission cannot be delivered and should not be retried."""

    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def is_batch_delivery_enabled():
    return bool(frappe.get_cached_doc("Feedback Delivery Settings").enable_batch_delivery)


def dispatch():
    """Start a feedback delivery run."""
    settings = frappe.get_cached_doc("Feedback Delivery Settings")
    if not settings.enable_batch_delivery:
        return

    stale_cutoff = add_to_date(now_datetime(), hours=-settings.run_stale_hours)
    active = frappe.db.exists(
        RUN_DOCTYPE, {"status": "Running", "modified": [">", stale_cutoff]}
    )
    if active:
        frappe.log_error(
            title=ERROR_LOG_TITLE,
            message=f"Feedback delivery window skipped: run {active} is still running. "
                    "Remaining submissions will go out at the next window.",
        )
        return

    run = frappe.get_doc(
        {
            "doctype": RUN_DOCTYPE,
            "status": "Running",
            "started_at": now_datetime(),
            "eligible_at_start": _count_eligible(settings),
        }
    ).insert(ignore_permissions=True)
    frappe.db.commit()
    _enqueue_chunk(run.name, None, "", settings)


# Frappe identifies Scheduled Job Type records by method name, so each cron
# expression needs a distinct callable even though all windows do the same work.
def dispatch_1500_ist():
    return dispatch()


def dispatch_1630_ist():
    return dispatch()


def dispatch_2100_ist():
    return dispatch()


def process_chunk(run_name, cursor_ts=None, cursor_name=""):
    """Deliver one page of eligible submissions, then chain to the next page."""
    settings = frappe.get_cached_doc("Feedback Delivery Settings")
    try:
        page = _fetch_page(settings, cursor_ts, cursor_name)
        for row in page:
            try:
                _deliver(row.name, settings)
            except Exception as exc:
                # The claim lease frees this submission for the next window.
                frappe.db.rollback()
                frappe.log_error(
                    title=ERROR_LOG_TITLE,
                    message=f"Unexpected error delivering feedback for {row.name}: "
                            f"{type(exc).__name__}: {exc}\n{frappe.get_traceback()}",
                )

        if len(page) == settings.batch_size:
            _enqueue_chunk(run_name, str(page[-1].sort_ts), page[-1].name, settings)
        else:
            _finish_run(run_name, "Completed", settings)
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(
            title=ERROR_LOG_TITLE,
            message=f"Feedback delivery page failed for run {run_name}: "
                    f"{type(exc).__name__}: {exc}\n{frappe.get_traceback()}",
        )
        _finish_run(run_name, "Incomplete", settings)
        raise


def _enqueue_chunk(run_name, cursor_ts, cursor_name, settings):
    frappe.enqueue(
        "tap_lms.imgana.feedback_delivery.process_chunk",
        queue="long",
        timeout=settings.job_timeout,
        run_name=run_name,
        cursor_ts=cursor_ts,
        cursor_name=cursor_name,
    )


def _finish_run(run_name, status, settings):
    started_at = frappe.db.get_value(RUN_DOCTYPE, run_name, "started_at")
    frappe.db.set_value(
        RUN_DOCTYPE,
        run_name,
        {
            "status": status,
            "finished_at": now_datetime(),
            "sent": frappe.db.count(
                "Submission", {"feedback_flow_triggered_at": [">=", started_at]}
            ),
            "remaining": _count_eligible(settings),
        },
    )
    frappe.db.commit()


def _eligible_params(settings):
    now = now_datetime()
    return {
        "max_attempts": settings.max_attempts,
        "lease_cutoff": add_to_date(now, minutes=-settings.lease_minutes),
        "oldest": add_to_date(now, days=-settings.max_age_days),
    }


def _count_eligible(settings):
    return frappe.db.sql(
        f"SELECT COUNT(*) FROM `tabSubmission` WHERE {_ELIGIBLE}",
        _eligible_params(settings),
    )[0][0]


def _fetch_page(settings, cursor_ts, cursor_name):
    return frappe.db.sql(
        f"""
        SELECT name, COALESCE(feedback_requested_at, creation) AS sort_ts
          FROM `tabSubmission`
         WHERE {_ELIGIBLE}
           AND (COALESCE(feedback_requested_at, creation), name)
               > (%(cursor_ts)s, %(cursor_name)s)
         ORDER BY sort_ts, name
         LIMIT %(limit)s
        """,
        {
            **_eligible_params(settings),
            "cursor_ts": cursor_ts or "1970-01-01 00:00:00",
            "cursor_name": cursor_name,
            "limit": settings.batch_size,
        },
        as_dict=True,
    )


def _claim(submission_id, settings):
    """Atomically take the submission for this attempt. False if someone else has it."""
    claimed = frappe.db.sql(
        """
        UPDATE `tabSubmission`
           SET feedback_claimed_at = %(now)s,
               feedback_delivery_attempts = COALESCE(feedback_delivery_attempts, 0) + 1
         WHERE name = %(name)s
           AND feedback_flow_triggered_at IS NULL
           AND (feedback_claimed_at IS NULL OR feedback_claimed_at < %(lease_cutoff)s)
        RETURNING name
        """,
        {
            "name": submission_id,
            "now": now_datetime(),
            "lease_cutoff": add_to_date(now_datetime(), minutes=-settings.lease_minutes),
        },
    )
    frappe.db.commit()
    return bool(claimed)


def _update(submission_id, **values):
    frappe.db.set_value("Submission", submission_id, values, update_modified=False)
    frappe.db.commit()


def _deliver(submission_id, settings):
    if not _claim(submission_id, settings):
        return

    sub = frappe.db.get_value(
        "Submission",
        submission_id,
        [
            "student_id",
            "feedback_flow_id",
            "overall_feedback",
            "overall_feedback_translated",
            "feedback_delivery_attempts",
        ],
        as_dict=True,
    )
    feedback = sub.overall_feedback or ""

    try:
        glific_id = _resolve_glific_id(submission_id, sub.student_id, settings)
        time.sleep(1 / settings.calls_per_second)
        started = start_contact_flow(
            flow_id=sub.feedback_flow_id,
            contact_id=glific_id,
            default_results={
                "submission_id": submission_id,
                "feedback": feedback,
                "overall_feedback": feedback,
                "overall_feedback_translated": sub.overall_feedback_translated or "",
            },
        )
        if not started:
            raise RuntimeError(
                f"start_contact_flow returned False (flow={sub.feedback_flow_id}, "
                f"contact={glific_id})"
            )
    except _Skip as skip:
        _update(
            submission_id,
            feedback_delivery_status=f"skipped:{skip.reason}",
            feedback_delivery_error=skip.detail[:500],
            feedback_claimed_at=None,
        )
    except Exception as exc:
        frappe.db.rollback()
        detail = f"{type(exc).__name__}: {exc}"
        values = {"feedback_delivery_error": detail[:500], "feedback_claimed_at": None}
        if sub.feedback_delivery_attempts >= settings.max_attempts:
            values["feedback_delivery_status"] = "parked"
            frappe.log_error(
                title=DLQ_LOG_TITLE,
                message=f"Submission {submission_id} parked after "
                        f"{sub.feedback_delivery_attempts} attempts: {detail}",
            )
        _update(submission_id, **values)
    else:
        _update(
            submission_id,
            feedback_flow_triggered_at=now_datetime(),
            feedback_claimed_at=None,
        )


def _format_phone(phone):
    """Same normalisation as glific_integration.update_student_glific_ids."""
    phone = (phone or "").strip().replace(" ", "")
    if len(phone) == 10:
        return f"91{phone}"
    if len(phone) == 12 and phone.startswith("91"):
        return phone
    return None


def _resolve_glific_id(submission_id, student_id, settings):
    """Return the Glific contact id. Raises _Skip if there is none to be found."""
    from tap_lms.feedback_handler.feedback_consumer import FeedbackConsumer

    # Reuse the consumer's id rules without constructing it (no RabbitMQ).
    consumer = FeedbackConsumer.__new__(FeedbackConsumer)

    comment_glific_id = consumer._get_submission_comment_glific_id(submission_id)
    if comment_glific_id:
        return comment_glific_id

    if not student_id:
        raise _Skip("no_student_id", "Submission has no student_id")

    if consumer._uses_direct_glific_contact_id(student_id):
        return student_id

    student = frappe.db.get_value(
        "Student", student_id, ["name", "phone", "glific_id"], as_dict=True
    )
    if not student:
        raise _Skip("student_not_found", f"Student {student_id} not found")

    if student.glific_id:
        return student.glific_id

    phone = _format_phone(student.phone)
    if not phone:
        raise _Skip(
            "invalid_phone",
            f"Student {student.name} has missing/invalid phone: {student.phone!r}",
        )

    time.sleep(1 / settings.calls_per_second)
    contact = get_contact_by_phone(phone)
    if not contact:
        raise _Skip(
            "no_matching_contact",
            f"No Glific contact for phone {phone} (student {student.name})",
        )

    entry = _parse_glific_fields_blob(contact.get("fields")).get("student_id")
    contact_student_id = entry.get("value") if isinstance(entry, dict) else entry
    if contact_student_id != student.name:
        raise _Skip(
            "no_matching_contact",
            f"Glific contact {contact['id']} for phone {phone} has "
            f"student_id={contact_student_id!r}, expected {student.name!r}",
        )

    frappe.db.set_value(
        "Student", student.name, "glific_id", contact["id"], update_modified=False
    )
    frappe.db.commit()
    return contact["id"]
