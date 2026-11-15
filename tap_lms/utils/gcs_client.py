import json
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import frappe
import requests
from google.cloud import storage

from tap_lms.imgana.media_detection import detect_url_media_type


@dataclass(frozen=True)
class GCSBucket:
    client: storage.Client
    name: str

    def blob(self, path: str):
        return self.client.bucket(self.name).blob(path)

    def public_url(self, path: str) -> str:
        return f"https://storage.googleapis.com/{self.name}/{path}"


def get_gcs_bucket(gcs_settings_name: str) -> GCSBucket | None:
    """
    Get GCS client using credentials from the named GCS Settings record.
    Returns a configured bucket or None if disabled or missing.
    """
    if not frappe.db.exists("GCS Settings", gcs_settings_name):
        return None

    settings = frappe.get_doc("GCS Settings", gcs_settings_name)
    if not settings.enabled:
        return None

    credentials_dict = json.loads(settings.credentials_json)
    client = storage.Client.from_service_account_info(credentials_dict)

    return GCSBucket(client, settings.bucket_name)


def get_content_type_from_response(response, filename, media_type):
    """
    Determine the correct content type from response headers or filename.
    Returns tuple of (content_type, file_extension)
    """
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()

    content_type_maps = {
        "image": {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
            "image/svg+xml": ".svg",
        },
        "video": {
            "video/mp4": ".mp4",
            "video/quicktime": ".mov",
            "video/x-msvideo": ".avi",
            "video/x-matroska": ".mkv",
            "video/webm": ".webm",
            "video/x-flv": ".flv",
            "video/x-ms-wmv": ".wmv",
        },
        "audio": {
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/ogg": ".ogg",
            "audio/mp4": ".m4a",
            "audio/aac": ".aac",
            "audio/webm": ".webm",
            "audio/flac": ".flac",
        },
    }

    default_content_types = {
        "image": ("image/jpeg", ".jpg"),
        "video": ("video/mp4", ".mp4"),
        "audio": ("audio/mpeg", ".mp3"),
    }

    content_type_map = content_type_maps.get(media_type, content_type_maps["image"])
    ext_to_content_type = {v: k for k, v in content_type_map.items()}
    if media_type == "image":
        ext_to_content_type[".jpeg"] = "image/jpeg"
    if media_type == "audio":
        ext_to_content_type[".opus"] = "audio/ogg"

    if content_type in content_type_map:
        return content_type, content_type_map[content_type]

    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext in ext_to_content_type:
            return ext_to_content_type[ext], ext

    return default_content_types.get(media_type, default_content_types["image"])


def upload_image_to_gcs(image_url, submission_id, bucket: GCSBucket):
    """
    Download image from external URL and upload to GCS.
    Returns the URL.
    """
    try:
        response = requests.get(image_url, timeout=30)
        response.raise_for_status()

        parsed_url = urlparse(image_url)
        original_filename = os.path.basename(parsed_url.path)
        content_type, ext = get_content_type_from_response(response, original_filename, "image")

        if not original_filename or "." not in original_filename:
            original_filename = f"image{ext}"

        gcs_filename = f"submissions/{submission_id}_{original_filename}"

        blob = bucket.blob(gcs_filename)
        blob.upload_from_string(response.content, content_type=content_type)

        url = bucket.public_url(gcs_filename)

        frappe.logger("submission").info(
            f"Image uploaded to GCS: {image_url} -> {url} (content_type: {content_type})"
        )

        return url

    except requests.exceptions.RequestException as e:
        frappe.logger("submission").error(f"Failed to download image from {image_url}: {str(e)}")
        raise frappe.ValidationError(f"Failed to download image: {str(e)}")
    except Exception as e:
        frappe.logger("submission").error(f"Failed to upload to GCS: {str(e)}")
        raise frappe.ValidationError(f"Failed to upload to GCS: {str(e)}")


def upload_audio_feedback_to_gcs(
    local_audio_path: str,
    submission_id: str,
    original_filename: str,
    gcs_settings_name: str,
) -> str:
    """
    Upload audio file from local path to GCS.
    Returns the public URL.
    """
    try:
        bucket = get_gcs_bucket(gcs_settings_name)

        if bucket is None:
            frappe.throw("Public GCS Storage is not enabled. Enable it in GCS Settings.")

        if not os.path.exists(local_audio_path):
            raise FileNotFoundError(f"Audio file not found at {local_audio_path}")

        ext = os.path.splitext(original_filename)[1].lower()
        content_type_map = {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
            ".m4a": "audio/mp4",
            ".aac": "audio/aac",
        }
        content_type = content_type_map.get(ext, "audio/mpeg")

        gcs_filename = f"audio_feedback/{submission_id}_{original_filename}"

        blob = bucket.blob(gcs_filename)

        with open(local_audio_path, "rb") as f:
            blob.upload_from_file(f, content_type=content_type)

        url = bucket.public_url(gcs_filename)

        frappe.logger("submission").info(
            f"Audio feedback uploaded to public GCS: {local_audio_path} -> {url} (content_type: {content_type})"
        )

        return url

    except FileNotFoundError as e:
        frappe.logger("submission").error(f"Audio file not found: {str(e)}")
        raise frappe.ValidationError(f"Audio file not found: {str(e)}")
    except Exception as e:
        frappe.logger("submission").error(f"Failed to upload audio to GCS: {str(e)}")
        raise frappe.ValidationError(f"Failed to upload audio to GCS: {str(e)}")


def upload_audio_to_gcs(audio_url: str, submission_id: str, bucket: GCSBucket) -> str:
    """
    Download audio from external URL and upload to GCS.
    Returns the URL.
    """
    try:
        response = requests.get(audio_url, timeout=60)
        response.raise_for_status()

        parsed_url = urlparse(audio_url)
        original_filename = os.path.basename(parsed_url.path)
        content_type, ext = get_content_type_from_response(response, original_filename, "audio")

        if not original_filename or "." not in original_filename:
            original_filename = f"audio{ext}"

        gcs_filename = f"submissions/{submission_id}_{original_filename}"

        blob = bucket.blob(gcs_filename)
        blob.upload_from_string(response.content, content_type=content_type)

        url = bucket.public_url(gcs_filename)

        frappe.logger("submission").info(
            f"Audio uploaded to GCS: {audio_url} -> {url} (content_type: {content_type})"
        )

        return url

    except requests.exceptions.RequestException as e:
        frappe.logger("submission").error(f"Failed to download audio from {audio_url}: {str(e)}")
        raise frappe.ValidationError(f"Failed to download audio: {str(e)}")
    except Exception as e:
        frappe.logger("submission").error(f"Failed to upload audio to GCS: {str(e)}")
        raise frappe.ValidationError(f"Failed to upload audio to GCS: {str(e)}")


def upload_video_to_gcs(video_url: str, submission_id: str, bucket: GCSBucket) -> str:
    """
    Download video from external URL and upload to GCS.
    Returns the URL.
    """
    try:
        response = requests.get(video_url, timeout=60, stream=True)
        response.raise_for_status()

        parsed_url = urlparse(video_url)
        original_filename = os.path.basename(parsed_url.path)
        content_type, ext = get_content_type_from_response(response, original_filename, "video")

        if not original_filename or "." not in original_filename:
            original_filename = f"video{ext}"

        gcs_filename = f"submissions/{submission_id}_{original_filename}"

        blob = bucket.blob(gcs_filename)
        blob.upload_from_string(response.content, content_type=content_type)

        url = bucket.public_url(gcs_filename)

        frappe.logger("submission").info(
            f"Video uploaded to GCS: {video_url} -> {url} (content_type: {content_type})"
        )

        return url

    except requests.exceptions.RequestException as e:
        frappe.logger("submission").error(f"Failed to download video from {video_url}: {str(e)}")
        raise frappe.ValidationError(f"Failed to download video: {str(e)}")
    except Exception as e:
        frappe.logger("submission").error(f"Failed to upload video to GCS: {str(e)}")
        raise frappe.ValidationError(f"Failed to upload video to GCS: {str(e)}")


def upload_to_gcs(submission_url, submission_name, gcs_settings_name, media_type=None):
    """
    Detect media type from the URL and upload to GCS.
    Returns the URL.
    """
    bucket = get_gcs_bucket(gcs_settings_name)
    if bucket is None:
        frappe.throw("GCS Storage is not enabled. Enable it in GCS Settings.")

    media_type = media_type or detect_url_media_type(submission_url, default="image")
    if media_type == "audio":
        return upload_audio_to_gcs(submission_url, submission_name, bucket)
    if media_type == "video":
        return upload_video_to_gcs(submission_url, submission_name, bucket)
    return upload_image_to_gcs(submission_url, submission_name, bucket)
