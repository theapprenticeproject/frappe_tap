from __future__ import annotations

import json
import re
from typing import Sequence
from uuid import uuid4

import frappe
import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account


GCP_CREDENTIALS_PROJECT_ID = "rubrics-data-migration"
GOOGLE_DRIVE_READWRITE_SCOPES = (
    "https://www.googleapis.com/auth/drive",
)
GOOGLE_DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
GOOGLE_DRIVE_READONLY_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
)


def get_google_service_account_info(project_id: str = GCP_CREDENTIALS_PROJECT_ID) -> dict:
    settings_name = frappe.db.get_value("GCS Settings", {"project_id": project_id}, "name")
    if not settings_name:
        frappe.throw(f"GCS Settings not found for project_id '{project_id}'")
    return json.loads(frappe.get_doc("GCS Settings", settings_name).credentials_json)


def _get_drive_session(scopes: Sequence[str]) -> AuthorizedSession:
    return AuthorizedSession(
        service_account.Credentials.from_service_account_info(
            get_google_service_account_info(),
            scopes=list(scopes),
        )
    )


def extract_google_drive_file_id(file_url: str) -> str | None:
    for pattern in (
        r"/spreadsheets/d/([a-zA-Z0-9-_]+)",
        r"/file/d/([a-zA-Z0-9-_]+)",
        r"[?&]id=([a-zA-Z0-9-_]+)",
    ):
        match = re.search(pattern, file_url)
        if match:
            return match.group(1)
    return None


def create_google_drive_folder(
    folder_name: str,
    parent_folder_id: str,
) -> dict[str, str]:
    session = _get_drive_session(GOOGLE_DRIVE_READWRITE_SCOPES)
    response = session.post(
        "https://www.googleapis.com/drive/v3/files",
        params={
            "fields": "id,name,webViewLink",
            "supportsAllDrives": "true",
        },
        json={
            "name": folder_name,
            "mimeType": GOOGLE_DRIVE_FOLDER_MIME_TYPE,
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
    session = _get_drive_session(GOOGLE_DRIVE_READWRITE_SCOPES)
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


def ensure_google_drive_folder(folder_name: str, parent_folder_id: str) -> str:
    session = _get_drive_session(GOOGLE_DRIVE_READWRITE_SCOPES)
    response = session.get(
        "https://www.googleapis.com/drive/v3/files",
        params={
            "q": (
                f"name = '{folder_name}' and '{parent_folder_id}' in parents "
                f"and mimeType = '{GOOGLE_DRIVE_FOLDER_MIME_TYPE}' and trashed = false"
            ),
            "fields": "files(id)",
            "pageSize": 1,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        },
        timeout=120,
    )
    response.raise_for_status()
    folders = response.json().get("files") or []
    if folders:
        return str(folders[0]["id"])
    return create_google_drive_folder(folder_name, parent_folder_id)["id"]


def download_private_workbook(file_id: str) -> requests.Response:
    session = _get_drive_session(GOOGLE_DRIVE_READONLY_SCOPES)
    response = session.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
        params={
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
        timeout=120,
    )
    if is_file_not_exportable_response(response):
        return _download_drive_file(session, file_id)
    if response.status_code == 403:
        raise frappe.ValidationError(_drive_access_error("sheet"))
    response.raise_for_status()
    return response


def download_private_drive_file(file_id: str) -> requests.Response:
    session = _get_drive_session(GOOGLE_DRIVE_READONLY_SCOPES)

    return _download_drive_file(session, file_id, supports_all_drives=True)


def _download_drive_file(
    session: AuthorizedSession,
    file_id: str,
    supports_all_drives: bool = False,
) -> requests.Response:
    params = {"alt": "media"}
    if supports_all_drives:
        params["supportsAllDrives"] = "true"
    response = session.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        params=params,
        timeout=120,
    )
    if response.status_code == 403:
        raise frappe.ValidationError(_drive_access_error("file"))
    response.raise_for_status()
    return response


def is_file_not_exportable_response(response: requests.Response) -> bool:
    if response.status_code != 403:
        return False
    try:
        payload = response.json()
    except Exception:
        return False
    errors = payload.get("error", {}).get("errors", [])
    return any(error.get("reason") == "fileNotExportable" for error in errors if isinstance(error, dict))


def _drive_access_error(file_type: str) -> str:
    service_account_email = get_google_service_account_info()["client_email"]
    return (
        f"Private Google Drive {file_type} access denied (403). "
        f"Share it with the service account '{service_account_email}' "
        f"from GCS Settings project_id '{GCP_CREDENTIALS_PROJECT_ID}', "
        "or verify that the Google Drive API is enabled for that project."
    )
