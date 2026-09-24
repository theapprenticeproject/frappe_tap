from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import frappe
from dateutil.parser import isoparse

from tap_lms.glific_integration import (
    _glific_post_with_401_retry,
    get_glific_settings,
)


GLIFIC_CONTACT_NOTIFICATION_CATEGORY = "Contact Upload"
GLIFIC_CONTACT_NOTIFICATION_POLL_INTERVAL_SECONDS = 120
GLIFIC_CONTACT_NOTIFICATION_TIMEOUT_SECONDS = 1200
GLIFIC_CONTACT_NOTIFICATION_QUERY_LIMIT = 50

GLIFIC_COLLECTION_BY_BATCH_ID = {
    "BT00000027": "TLM26_AllStudents_Batch MCD",
    "BT00000026": "TLM26_AllStudents_Batch Moga_P",
    "BT00000025": "TLM26_AllStudents_Batch2",
    "BT00000024": "TLM26_AllStudents_Batch1",
    "BT00000029": "TLM26_AllStudents_Batch UP",
}
GLIFIC_COLLECTION_WITHOUT_COURSE_BY_BATCH_ID = {
    "BT00000029": "CourseSelection_Batch_UP",
}
GLIFIC_COLLECTION_WITHOUT_COURSE = "TLM26_AllStudents_APS"

MOVE_CONTACTS_MUTATION = """
mutation MoveContacts($data: String!, $type: ImportContactsTypeEnum) {
  moveContacts(data: $data, type: $type) {
    status
    errors {
      key
      message
    }
  }
}
"""

CONTACT_UPLOAD_NOTIFICATIONS_QUERY = """
query Notifications($filter: NotificationFilter, $opts: Opts) {
  notifications(filter: $filter, opts: $opts) {
    id
    category
    entity
    message
    severity
    insertedAt
    updatedAt
    isRead
  }
}
"""


def get_glific_collection(batch_id: str, course: str) -> str:
    if str(course or "").strip():
        return GLIFIC_COLLECTION_BY_BATCH_ID.get(str(batch_id or "").strip(), "")
    return GLIFIC_COLLECTION_WITHOUT_COURSE_BY_BATCH_ID.get(
        str(batch_id or "").strip(),
        GLIFIC_COLLECTION_WITHOUT_COURSE,
    )


def move_contacts_and_wait(
    csv_text: str,
    references: dict[str, list[str]],
) -> dict:
    started_at = datetime.now(timezone.utc)
    move_status, move_response = _move_contacts(csv_text)
    notification_result = _poll_contact_upload_completion(started_at, references)
    return {
        "move_status": move_status,
        "move_started_at": started_at.isoformat(),
        "glific_response": move_response,
        "notification_status": notification_result.get("status") or "",
        "user_job_id": notification_result.get("user_job_id"),
        "notification_result": notification_result,
    }


def _move_contacts(csv_text: str) -> tuple[str, dict]:
    body = _post_glific_graphql({
        "query": MOVE_CONTACTS_MUTATION,
        "variables": {"data": csv_text, "type": "DATA"},
    })
    result = body.get("data", {}).get("moveContacts")
    if not isinstance(result, dict):
        raise frappe.ValidationError(
            "Glific response did not contain data.moveContacts: "
            + json.dumps(body, ensure_ascii=False)
        )
    if result.get("errors"):
        raise frappe.ValidationError(
            "Glific moveContacts failed: "
            + json.dumps(result["errors"], ensure_ascii=False)
        )

    status = str(result.get("status") or "").strip()
    if not status:
        raise frappe.ValidationError("Glific moveContacts response had no status")
    return status, body


def _post_glific_graphql(payload: dict) -> dict:
    settings = get_glific_settings()
    response = _glific_post_with_401_retry(
        f"{str(settings.api_url).rstrip('/')}/api",
        payload,
    )
    body = response.json()
    if body.get("errors"):
        raise frappe.ValidationError(
            "Glific GraphQL error: "
            + json.dumps(body["errors"], ensure_ascii=False)
        )
    return body


def _poll_contact_upload_completion(
    started_at: datetime,
    references: dict[str, list[str]],
) -> dict:
    deadline = time.monotonic() + GLIFIC_CONTACT_NOTIFICATION_TIMEOUT_SECONDS
    timestamp_floor = started_at.replace(microsecond=0)
    target_job_id = ""
    last_matching_notifications: list[dict] = []
    poll_count = 0

    while time.monotonic() <= deadline:
        poll_count += 1
        recent = []
        for notification in _query_contact_upload_notifications():
            inserted_at = _notification_inserted_at(notification)
            if inserted_at and inserted_at >= timestamp_floor:
                recent.append(notification)

        if target_job_id:
            matching = [
                notification
                for notification in recent
                if _contact_upload_job_id(notification) == target_job_id
            ]
        else:
            referenced = [
                notification
                for notification in recent
                if _contact_upload_job_id(notification)
                and _notification_references_contact_csv(notification, references)
            ]
            candidates = referenced or [
                notification
                for notification in recent
                if _contact_upload_job_id(notification)
                and "contact upload"
                in str(notification.get("message") or "").strip().lower()
            ]
            candidate_job_ids = {
                _contact_upload_job_id(notification)
                for notification in candidates
            }
            candidate_job_ids.discard("")
            if len(candidate_job_ids) > 1:
                raise frappe.ValidationError(
                    "Multiple new Glific Contact Upload jobs were found; "
                    "cannot identify the job for this CSV"
                )
            if len(candidate_job_ids) == 1:
                target_job_id = next(iter(candidate_job_ids))
                matching = [
                    notification
                    for notification in recent
                    if _contact_upload_job_id(notification) == target_job_id
                ]
            else:
                matching = []

        if matching:
            last_matching_notifications = matching
            completed = next(
                (
                    notification
                    for notification in matching
                    if "completed"
                    in str(notification.get("message") or "").strip().lower()
                ),
                None,
            )
            if completed:
                return {
                    "status": "completed",
                    "user_job_id": target_job_id,
                    "poll_count": poll_count,
                    "notification": completed,
                }

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(
            min(GLIFIC_CONTACT_NOTIFICATION_POLL_INTERVAL_SECONDS, remaining)
        )

    return {
        "status": "timeout",
        "user_job_id": target_job_id or None,
        "poll_count": poll_count,
        "timeout_seconds": GLIFIC_CONTACT_NOTIFICATION_TIMEOUT_SECONDS,
        "last_matching_notifications": last_matching_notifications,
    }


def _query_contact_upload_notifications() -> list[dict]:
    body = _post_glific_graphql({
        "query": CONTACT_UPLOAD_NOTIFICATIONS_QUERY,
        "variables": {
            "filter": {"category": GLIFIC_CONTACT_NOTIFICATION_CATEGORY},
            "opts": {
                "order": "DESC",
                "orderWith": "inserted_at",
                "limit": GLIFIC_CONTACT_NOTIFICATION_QUERY_LIMIT,
                "offset": 0,
            },
        },
    })
    notifications = body.get("data", {}).get("notifications")
    if not isinstance(notifications, list):
        raise frappe.ValidationError(
            "Glific response did not contain data.notifications: "
            + json.dumps(body, ensure_ascii=False)
        )
    return notifications


def _parse_notification_entity(notification: dict) -> dict:
    entity = notification.get("entity")
    if isinstance(entity, dict):
        return entity
    if isinstance(entity, str):
        try:
            parsed = json.loads(entity)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _contact_upload_job_id(notification: dict) -> str:
    entity = _parse_notification_entity(notification)
    return str(entity.get("user_job_id") or entity.get("userJobId") or "").strip()


def _notification_references_contact_csv(
    notification: dict,
    references: dict[str, list[str]],
) -> bool:
    entity_text = json.dumps(
        _parse_notification_entity(notification),
        ensure_ascii=False,
    ).lower()
    values = references.get("phones", []) + references.get("collections", [])
    return any(str(value).lower() in entity_text for value in values if value)


def _notification_inserted_at(notification: dict) -> datetime | None:
    value = str(notification.get("insertedAt") or "").strip()
    if not value:
        return None
    try:
        parsed = isoparse(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
