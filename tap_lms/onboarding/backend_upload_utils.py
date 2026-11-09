from __future__ import annotations

import csv
import json
from io import StringIO
from typing import Sequence
from uuid import uuid4

import frappe
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage
from google.oauth2 import service_account


GCP_CREDENTIALS_PROJECT_ID = "rubrics-data-migration"
FAILED_ROWS_GCP_PROJECT_ID = "central-phalanx-297915"
GLIFIC_CONTACTS_FOLDER = "Glific_contacts"
STUDENT_BULK_IMPORT_DRIVE_FOLDER_ID = "1__wcgwdQT0heEpf-g0vhfL8lRTb_jkG8"
GLIFIC_NEW_STUDENT_CSV_HEADERS = [
    "name",
    "phone",
    "language",
    "delete",
    "school_id",
    "state",
    "model",
    "buddy_name",
    "batch_id",
    "grade",
    "level",
    "course",
    "student_id",
]
GLIFIC_EXISTING_STUDENT_CSV_HEADERS = [
    "name",
    "phone",
    "school_id",
    "state",
    "model",
    "buddy_name",
    "batch_id",
    "grade",
    "level",
    "course",
    "student_id",
]
GLIFIC_CSV_HEADERS = GLIFIC_NEW_STUDENT_CSV_HEADERS

GOOGLE_DRIVE_READONLY_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
)
GOOGLE_SHEETS_READWRITE_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
)
GOOGLE_DRIVE_READWRITE_SCOPES = (
    "https://www.googleapis.com/auth/drive",
)


def get_gcs_settings(project_id: str):
    settings_name = frappe.db.get_value(
        "GCS Settings",
        {"project_id": project_id},
        "name",
    )
    if not settings_name:
        frappe.throw(f"GCS Settings not found for project_id '{project_id}'")
    return frappe.get_doc("GCS Settings", settings_name)


def get_google_service_account_credentials(
    project_id: str = GCP_CREDENTIALS_PROJECT_ID,
    scopes: Sequence[str] | None = None,
):
    settings = get_gcs_settings(project_id)
    credentials_dict = json.loads(settings.credentials_json)
    return service_account.Credentials.from_service_account_info(
        credentials_dict,
        scopes=list(scopes or GOOGLE_DRIVE_READONLY_SCOPES),
    )


def get_google_service_account_email(project_id: str = GCP_CREDENTIALS_PROJECT_ID) -> str:
    settings_name = frappe.db.get_value(
        "GCS Settings",
        {"project_id": project_id},
        "name",
    )
    if not settings_name:
        return ""

    settings = frappe.get_doc("GCS Settings", settings_name)
    try:
        credentials_dict = json.loads(settings.credentials_json)
    except Exception:
        return ""
    return str(credentials_dict.get("client_email") or "").strip()


def create_google_drive_folder(
    folder_name: str,
    parent_folder_id: str = STUDENT_BULK_IMPORT_DRIVE_FOLDER_ID,
) -> dict[str, str]:
    session = AuthorizedSession(
        get_google_service_account_credentials(scopes=GOOGLE_DRIVE_READWRITE_SCOPES)
    )
    response = session.post(
        "https://www.googleapis.com/drive/v3/files",
        params={
            "fields": "id,name,webViewLink",
            "supportsAllDrives": "true",
        },
        json={
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_folder_id],
        },
        timeout=120,
    )
    response.raise_for_status()
    folder = response.json()
    folder_id = str(folder["id"])
    return {
        "id": folder_id,
        "name": str(folder.get("name") or folder_name),
        "url": str(folder.get("webViewLink") or f"https://drive.google.com/drive/folders/{folder_id}"),
    }


def upload_bytes_to_google_drive(
    content: bytes,
    file_name: str,
    parent_folder_id: str,
    content_type: str,
) -> str:
    session = AuthorizedSession(
        get_google_service_account_credentials(scopes=GOOGLE_DRIVE_READWRITE_SCOPES)
    )
    boundary = f"codex_{uuid4().hex}"
    metadata = json.dumps({
        "name": file_name,
        "mimeType": content_type,
        "parents": [parent_folder_id],
    }).encode("utf-8")
    body = b"\r\n".join([
        f"--{boundary}".encode("ascii"),
        b"Content-Type: application/json; charset=UTF-8",
        b"",
        metadata,
        f"--{boundary}".encode("ascii"),
        f"Content-Type: {content_type}".encode("ascii"),
        b"",
        content,
        f"--{boundary}--".encode("ascii"),
        b"",
    ])
    response = session.post(
        "https://www.googleapis.com/upload/drive/v3/files",
        params={
            "uploadType": "multipart",
            "fields": "id,name,webViewLink",
            "supportsAllDrives": "true",
        },
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        data=body,
        timeout=120,
    )
    response.raise_for_status()
    uploaded_file = response.json()
    file_id = str(uploaded_file["id"])
    return str(
        uploaded_file.get("webViewLink")
        or f"https://drive.google.com/file/d/{file_id}/view"
    )


def upload_bytes_to_gcs(
    content: bytes,
    object_name: str,
    project_id: str,
    content_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
) -> str:
    settings = get_gcs_settings(project_id)
    credentials_dict = json.loads(settings.credentials_json)
    client = storage.Client.from_service_account_info(credentials_dict)
    bucket = client.bucket(settings.bucket_name)
    blob = bucket.blob(object_name)
    blob.upload_from_string(content, content_type=content_type)
    return f"https://storage.cloud.google.com/{settings.bucket_name}/{object_name}"


def upload_glific_contact_csv(
    file_name: str,
    rows: list[dict],
    project_id: str = FAILED_ROWS_GCP_PROJECT_ID,
    headers: Sequence[str] | None = None,
) -> str:
    return upload_bytes_to_gcs(
        render_glific_contact_csv(rows, headers=headers),
        f"{GLIFIC_CONTACTS_FOLDER}/{file_name}",
        project_id,
        content_type="text/csv",
    )


def render_glific_contact_csv(
    rows: list[dict],
    headers: Sequence[str] | None = None,
) -> bytes:
    fieldnames = list(headers or GLIFIC_CSV_HEADERS)
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows([
        {header: _csv_value(row.get(header)) for header in fieldnames}
        for row in rows
    ])
    return buffer.getvalue().encode("utf-8")


def _csv_value(value: object) -> str:
    return "" if value is None else str(value)
