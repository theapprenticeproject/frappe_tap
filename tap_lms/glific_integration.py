import frappe
import requests
import json
from datetime import datetime, timedelta, timezone
from dateutil.parser import isoparse

# ── CR-004 Slice 0: shared session + explicit timeout on every Glific call ──
# A module-level Session reuses the TLS connection across calls (keep-alive).
# GLIFIC_TIMEOUT is a hard ceiling on connect+read combined; without it a
# hung Glific endpoint blocks the entire RQ worker thread indefinitely,
# causing supervisor STOPPING / orphaned-worker incidents (2026-05-31).
_GLIFIC_SESSION = requests.Session()
GLIFIC_TIMEOUT = 10  # seconds, connect+read combined

def get_glific_settings():
    return frappe.get_single("Glific Settings")


# ── CR-025: Token-invalidation helper ─────────────────────────────────────────
# Clears the stored access_token and token_expiry_time in Glific Settings so
# the NEXT call to get_glific_auth_headers() triggers a fresh login (re-fetch
# from the phone/password credentials). Called when any API POST returns 401.
#
# Uses frappe.db.set_value with update_modified=False (L-039 pattern):
#   - No save-hook chain fires (correct — Glific Settings is a singleton with
#     no Glific-mapped fields; no reconcile needed per the L-039 exception).
#   - Followed by frappe.db.commit() so the cleared token is visible to other
#     workers that might also be retrying concurrent calls.
#
# IMPORTANT: This is NOT called from get_glific_auth_headers() itself (the
# auth POST). It is ONLY called from _glific_post_with_401_retry() which wraps
# API POSTs. The auth POST is never wrapped (else infinite recursion).

def _invalidate_stored_token():
    """Clear the cached Glific token to force re-authentication on next call.

    After this returns, get_glific_auth_headers() will POST /api/v1/session
    and store a fresh token.
    """
    settings = get_glific_settings()
    frappe.db.set_value(
        "Glific Settings",
        settings.name,
        {"access_token": None, "token_expiry_time": None},
        update_modified=False,
    )
    frappe.db.commit()


# ── CR-025: 401-resilient POST helper ─────────────────────────────────────────
# Routes every API POST (NOT the auth POST) through a single helper that:
#   1. Fetches fresh auth headers for EACH attempt (so callers stop passing
#      headers and headers never go stale between retries).
#   2. On HTTP 401: invalidates the cached token + refetches headers + retries
#      ONCE. A second 401 raises raise_for_status() immediately — no loop.
#   3. On any other non-2xx: raises raise_for_status() immediately.
#
# Pattern:
#   OLD: response = _GLIFIC_SESSION.post(url, json=payload, headers=get_glific_auth_headers(), timeout=GLIFIC_TIMEOUT)
#        response.raise_for_status()
#   NEW: response = _glific_post_with_401_retry(url, payload)
#
# The helper NEVER wraps the auth POST itself (/api/v1/session call inside
# get_glific_auth_headers). To guard against that, callers must pass only
# the GraphQL /api endpoint — auth is auto-wired internally.

def _glific_post_with_401_retry(url, payload, max_attempts=2):
    """POST to a Glific API endpoint with automatic 401-token-refresh retry.

    Args:
        url: Full Glific API URL (must be /api, NOT /api/v1/session).
        payload: dict — JSON body for the POST request.
        max_attempts: Number of total attempts (default 2 — one fresh-headers
                      attempt + one retry after token invalidation).

    Returns:
        requests.Response with status_code in the 2xx range.

    Raises:
        requests.HTTPError: if the response is non-2xx after all attempts,
                            or if a 401 persists after token refresh.
        requests.RequestException: on network errors (connect timeout, etc.).
    """
    last_response = None
    for attempt in range(1, max_attempts + 1):
        headers = get_glific_auth_headers()
        resp = _GLIFIC_SESSION.post(url, json=payload, headers=headers,
                                    timeout=GLIFIC_TIMEOUT)
        if resp.status_code == 401:
            frappe.logger().warning(
                f"_glific_post_with_401_retry: 401 on attempt {attempt} "
                f"for {url}; invalidating token."
            )
            # Invalidate on EVERY 401, including the terminal attempt. The
            # final invalidation is a cheap idempotent no-op on the already-
            # cleared token, but it guarantees the confirmed-dead token is gone
            # so the NEXT operation re-authenticates instead of reusing it.
            _invalidate_stored_token()
            last_response = resp
            # If this was the last attempt, fall through to raise_for_status below
            if attempt < max_attempts:
                continue
        elif not resp.ok:
            # Non-401 error — surface immediately, no retry benefit
            resp.raise_for_status()
        else:
            return resp  # Success

    # All attempts consumed — raise on the final bad response
    last_response.raise_for_status()
    return last_response  # unreachable but satisfies linters


def _to_naive_utc(value):
    """Normalise a token expiry (str or datetime) to a naive UTC datetime.

    token_expiry_time is always stored as naive UTC. Frappe Datetime fields
    carry no timezone (Single values come back as strings) and the site runs
    on IST, so an aware value would lose its offset on a Desk form save and
    break the comparison. Aware inputs are converted to UTC; naive inputs are
    assumed to already be UTC.
    """
    if isinstance(value, str):
        value = isoparse(value)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def get_glific_auth_headers():
    settings = get_glific_settings()
    current_time = datetime.now(timezone.utc).replace(tzinfo=None)

    token_expiry_time = (
        _to_naive_utc(settings.token_expiry_time) if settings.token_expiry_time else None
    )

    if not settings.access_token or not token_expiry_time or \
       current_time >= token_expiry_time:
        # Token is expired or not set, get a new one
        url = f"{settings.api_url}/api/v1/session"
        payload = {
            "user": {
                "phone": settings.phone_number,
                "password": settings.password
            }
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        response = _GLIFIC_SESSION.post(url, json=payload, headers=headers,
                                        timeout=GLIFIC_TIMEOUT)
        if response.status_code == 200:
            data = response.json()["data"]

            # Store as naive UTC (see _to_naive_utc)
            token_expiry_time = _to_naive_utc(data["token_expiry_time"])
            
            # Update the Glific Settings directly in the database
            frappe.db.set_value("Glific Settings", settings.name, {
                "access_token": data["access_token"],
                "renewal_token": data["renewal_token"],
                "token_expiry_time": token_expiry_time
            }, update_modified=False)
            
            frappe.db.commit()
            
            return {
                "authorization": data["access_token"],
                "Content-Type": "application/json"
            }
        else:
            frappe.throw("Failed to authenticate with Glific API")
    else:
        return {
            "authorization": settings.access_token,
            "Content-Type": "application/json"
        }

def create_contact(
    name,
    phone,
    school_name,
    model_name,
    language_id,
    batch_id,
    extra_fields=None,
    include_default_school_field=True,
):
    settings = get_glific_settings()
    url = f"{settings.api_url}/api"
    headers = get_glific_auth_headers()

    # Prepare the fields dictionary
    fields = {
        "model": {
            "value": model_name,
            "type": "string",
            "inserted_at": datetime.now(timezone.utc).isoformat()
        },
        "buddy_name": {
            "value": name,
            "type": "string",
            "inserted_at": datetime.now(timezone.utc).isoformat()
        },
        "batch_id": {
            "value": batch_id,
            "type": "string",
            "inserted_at": datetime.now(timezone.utc).isoformat()
        }
    }
    if include_default_school_field:
        fields["school"] = {
            "value": school_name,
            "type": "string",
            "inserted_at": datetime.now(timezone.utc).isoformat()
        }
    for fieldname, value in (extra_fields or {}).items():
        fields[fieldname] = {
            "value": str(value),
            "type": "string",
            "inserted_at": datetime.now(timezone.utc).isoformat()
        }

    payload = {
        "query": "mutation createContact($input:ContactInput!) { createContact(input: $input) { contact { id name phone } errors { key message } } }",
        "variables": {
            "input": {
                "name": name,
                "phone": phone,
                "fields": json.dumps(fields),
                "languageId": int(language_id)
            }
        }
    }

    frappe.logger().info(f"Attempting to create Glific contact. Name: {name}, Phone: {phone}, School: {school_name}, Model: {model_name}, Language ID: {language_id}, Batch ID: {batch_id}")
    frappe.logger().info(f"Glific API URL: {url}")
    frappe.logger().info(f"Glific API Payload: {payload}")

    try:
        # CR-025: use 401-retry helper; headers fetched internally per attempt
        response = _glific_post_with_401_retry(url, payload)
        frappe.logger().info(f"Glific API response status: {response.status_code}")
        frappe.logger().info(f"Glific API response content: {response.text}")

        if response.status_code == 200:
            data = response.json()
            if "errors" in data:
                frappe.logger().error(f"Error creating Glific contact: {data['errors']}")
                return None
            if "data" in data and "createContact" in data["data"] and "contact" in data["data"]["createContact"]:
                contact = data["data"]["createContact"]["contact"]
                frappe.logger().info(f"Glific contact created successfully: {contact}")
                return contact
            else:
                frappe.logger().error(f"Unexpected response structure: {data}")
                return None
        else:
            frappe.logger().error(f"Failed to create Glific contact. Status code: {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        frappe.logger().error(f"Network error creating Glific contact: {str(e)}", exc_info=True)
        raise  # FIX 2: transient network errors must propagate
    except Exception as e:
        frappe.logger().error(f"Exception occurred while creating Glific contact: {str(e)}", exc_info=True)
        return None

def _parse_glific_fields_blob(raw_fields):
    """Best-effort parse for Glific contact fields JSON."""
    if not raw_fields:
        return {}
    if isinstance(raw_fields, dict):
        return raw_fields
    try:
        return json.loads(raw_fields)
    except (json.JSONDecodeError, TypeError):
        return {}


def _glific_fields_match(updated_fields, fields_to_update):
    """Return True only if every requested key is reflected in Glific."""
    parsed_fields = _parse_glific_fields_blob(updated_fields)
    for key, expected_value in (fields_to_update or {}).items():
        actual_entry = parsed_fields.get(key)
        if isinstance(actual_entry, dict):
            actual_value = actual_entry.get("value")
        else:
            actual_value = actual_entry

        if str(actual_value or "") != str(expected_value or ""):
            return False
    return True


def _fetch_glific_contact(contact_id, url):
    """Fetch a single Glific contact for read/verify flows."""
    fetch_payload = {
        "query": """
        query contact($id: ID!) {
          contact(id: $id) {
            contact {
              id
              name
              language {
                id
              }
              fields
            }
          }
        }
        """,
        "variables": {"id": str(contact_id)},
    }

    fetch_response = _glific_post_with_401_retry(url, fetch_payload)
    fetch_data = fetch_response.json()

    if "errors" in fetch_data:
        return None, fetch_data["errors"]

    return fetch_data.get("data", {}).get("contact", {}).get("contact"), None


def _glific_language_matches(contact_data, language_id):
    """Return True only if the fetched contact reflects the requested language."""
    if language_id is None or language_id == "":
        return True

    try:
        expected_language_id = str(int(language_id))
    except (TypeError, ValueError):
        return True

    actual_language = (contact_data or {}).get("language") or {}
    return str(actual_language.get("id") or "") == expected_language_id


def _glific_name_matches(contact_data, contact_name):
    """Return True only if the fetched contact reflects the requested name."""
    if contact_name is None:
        return True
    return str((contact_data or {}).get("name") or "").strip() == str(contact_name or "").strip()


def _glific_errors_indicate_missing_contact(errors):
    """Best-effort detect 'contact not found' mutation failures."""
    for err in errors or []:
        key = str((err or {}).get("key") or "").lower()
        msg = str((err or {}).get("message") or "").lower()
        text = f"{key} {msg}"
        if "not found" in text or "no contact" in text or "record not found" in text:
            return True
    return False


def _create_missing_glific_contact(create_contact_input, language_id, fields_to_update):
    """Create a new Glific contact when the target id no longer exists."""
    create_contact_input = create_contact_input or {}
    contact_name = create_contact_input.get("name")
    phone = create_contact_input.get("phone")
    school_name = create_contact_input.get("school_name") or fields_to_update.get("school") or ""
    model_name = create_contact_input.get("model_name") or fields_to_update.get("model") or ""
    batch_id = create_contact_input.get("batch_id")
    if batch_id is None:
        batch_id = fields_to_update.get("batch_id", "")

    if not contact_name or not phone:
        return None

    return create_contact(
        contact_name,
        phone,
        school_name,
        model_name,
        language_id,
        batch_id,
        fields_to_update,
    )


def _set_glific_sync_status(doctype, docname, status):
    """Write glific_sync_status only for doctypes that expose the field."""
    if not doctype or not docname:
        return

    try:
        meta = frappe.get_meta(doctype)
        if not meta.has_field("glific_sync_status"):
            return
        frappe.db.set_value(
            doctype,
            docname,
            "glific_sync_status",
            status,
            update_modified=False,
        )
    except Exception as exc:
        frappe.logger().warning(
            f"Failed to set glific_sync_status={status} for {doctype} {docname}: {exc}"
        )


def update_contact_fields(
    contact_id,
    fields_to_update,
    language_id=None,
    contact_name=None,
    sync_status_doctype=None,
    sync_status_docname=None,
    create_contact_input=None,
    existing_fields=None,
    fields_to_remove=None,
):
    """
    Update Glific contact fields directly, optionally updating the contact's
    CORE language at the same time, then verify via a post-write fetch.

    Uses 2 GraphQL calls in the normal path:
      1. updateContact
      2. Fetch the contact again and verify the write persisted remotely

    If the target contact id no longer exists and `create_contact_input` is
    supplied, this helper creates a replacement contact, retries the update
    against the new id, then verifies via fetch.

    Args:
        contact_id: Glific contact ID (string or int)
        fields_to_update: dict of {field_name: value} to set. Can be empty
                          if you only want to update language_id.
        language_id: Optional Glific INTEGER language ID. When provided, sets
                     the contact's CORE `language` field as part of the same
                     updateContact mutation — no extra network round-trip.
                     Pass None (default) to skip core-language update; pass
                     an integer (or numeric string) to set it. Distinct from
                     the custom `language_id` contact field — this updates
                     Glific's built-in language attribute. Added 2026-05-19
                     to fix the existing-contact-language-not-updated gap.
        contact_name: Optional Glific core contact name to set as part of the
                     same updateContact mutation and verify after write.

        create_contact_input: Optional dict with `name`, `phone`, `school_name`,
                     `model_name`, and optional `batch_id`. Used only when the
                     target contact id is missing and a replacement contact
                     must be created before retrying the update.
        fields_to_remove: Optional iterable of custom contact field keys to
                     remove from the outgoing fields blob before updating.

    Returns:
        True on success, False on failure
    """
    settings = get_glific_settings()
    url = f"{settings.api_url}/api"

    try:
        if isinstance(fields_to_remove, str):
            fields_to_remove = (fields_to_remove,)

        # ── Step 1: Build direct update payload ────────────────
        outgoing_fields = _parse_glific_fields_blob(existing_fields)
        for key in fields_to_remove or ():
            outgoing_fields.pop(key, None)
        for key, value in fields_to_update.items():
            outgoing_fields[key] = {
                "value": str(value),
                "type": "string",
                "inserted_at": datetime.now(timezone.utc).isoformat()
            }

        # ── Step 2: Write fields directly ──────────────────────
        # If language_id was passed, include it in the mutation input so
        # Glific's CORE language attribute is updated alongside the custom
        # fields blob — single round-trip.
        mutation_input = {
            "fields": json.dumps(outgoing_fields),
        }
        if contact_name is not None:
            mutation_input["name"] = str(contact_name or "").strip()
        if language_id is not None and language_id != "":
            try:
                mutation_input["languageId"] = int(language_id)
            except (TypeError, ValueError):
                frappe.logger().warning(
                    f"Glific update_contact_fields: language_id={language_id!r} "
                    f"is not a valid integer; skipping core-language update "
                    f"for contact {contact_id}."
                )

        update_payload = {
            "query": """
            mutation updateContact($id: ID!, $input: ContactInput!) {
              updateContact(id: $id, input: $input) {
                contact {
                  id
                  fields
                }
                errors {
                  key
                  message
                }
              }
            }
            """,
            "variables": {
                "id": str(contact_id),
                "input": mutation_input,
            },
        }

        # CR-025: 401-retry helper fetches headers internally; no headers arg
        update_response = _glific_post_with_401_retry(url, update_payload)
        update_data = update_response.json()

        if "errors" in update_data:
            frappe.logger().error(f"Glific updateContact error for {contact_id}: {update_data['errors']}")
            _set_glific_sync_status(sync_status_doctype, sync_status_docname, "failed")
            return False

        result = update_data.get("data", {}).get("updateContact", {})
        result_errors = result.get("errors") or []
        if result_errors:
            if _glific_errors_indicate_missing_contact(result_errors) and create_contact_input:
                new_contact = _create_missing_glific_contact(
                    create_contact_input,
                    language_id,
                    fields_to_update,
                )
                if not new_contact or not new_contact.get("id"):
                    frappe.logger().error(
                        f"Glific missing-contact recovery create failed for {contact_id}: "
                        f"{result_errors}"
                    )
                    _set_glific_sync_status(sync_status_doctype, sync_status_docname, "failed")
                    return False

                contact_id = str(new_contact["id"])
                if sync_status_doctype and sync_status_docname:
                    frappe.db.set_value(
                        sync_status_doctype,
                        sync_status_docname,
                        "glific_id",
                        contact_id,
                        update_modified=False,
                    )

                update_payload["variables"]["id"] = contact_id
                retry_response = _glific_post_with_401_retry(url, update_payload)
                update_data = retry_response.json()
                if "errors" in update_data:
                    frappe.logger().error(
                        f"Glific updateContact error after create for {contact_id}: "
                        f"{update_data['errors']}"
                    )
                    _set_glific_sync_status(sync_status_doctype, sync_status_docname, "failed")
                    return False
                result = update_data.get("data", {}).get("updateContact", {})
                result_errors = result.get("errors") or []

            if result_errors:
                frappe.logger().error(
                    f"Glific updateContact mutation error for {contact_id}: {result_errors}"
                )
                _set_glific_sync_status(sync_status_doctype, sync_status_docname, "failed")
                return False

        if not result.get("contact"):
            frappe.logger().error(
                f"Glific updateContact unexpected response for {contact_id}: {update_data}"
            )
            _set_glific_sync_status(sync_status_doctype, sync_status_docname, "failed")
            return False

        # ── Step 3: Fetch-after-write verification ────────────
        verified_contact, verify_errors = _fetch_glific_contact(contact_id, url)
        if verify_errors:
            frappe.logger().error(
                f"Glific post-update fetch error for {contact_id}: {verify_errors}"
            )
            _set_glific_sync_status(sync_status_doctype, sync_status_docname, "failed")
            return False

        fields_match = _glific_fields_match(
            (verified_contact or {}).get("fields"),
            fields_to_update,
        )
        verified_fields = _parse_glific_fields_blob(
            (verified_contact or {}).get("fields")
        )
        removed_fields_absent = all(
            key not in verified_fields
            for key in fields_to_remove or ()
        )
        language_match = _glific_language_matches(verified_contact, language_id)
        name_match = _glific_name_matches(verified_contact, contact_name)

        if fields_match and removed_fields_absent and language_match and name_match:
            _set_glific_sync_status(sync_status_doctype, sync_status_docname, "synced")
            return True

        if verified_contact:
            frappe.logger().error(
                f"Glific updateContact verification failed for {contact_id}: "
                f"requested_fields={fields_to_update}, requested_language_id={language_id}, "
                f"requested_removed_fields={fields_to_remove}, "
                f"requested_name={contact_name}, returned_name={verified_contact.get('name')}, "
                f"returned_language={verified_contact.get('language')}, "
                f"returned_fields={verified_contact.get('fields')}"
            )
            _set_glific_sync_status(sync_status_doctype, sync_status_docname, "failed")
            return False

        frappe.logger().error(
            f"Glific updateContact verification returned no contact for {contact_id}"
        )
        _set_glific_sync_status(sync_status_doctype, sync_status_docname, "failed")
        return False

    except requests.exceptions.RequestException as e:
        frappe.logger().error(f"Glific API request error for contact {contact_id}: {str(e)}")
        _set_glific_sync_status(sync_status_doctype, sync_status_docname, "failed")
        raise  # FIX 2: transient network errors must propagate
    except Exception as e:
        frappe.logger().error(f"Glific update_contact_fields error for {contact_id}: {str(e)}")
        _set_glific_sync_status(sync_status_doctype, sync_status_docname, "failed")
        return False


# ════════════════════════════════════════════════════════════
# CONTACT FIELD DEFINITION (createContactsField)
# ════════════════════════════════════════════════════════════
# Added 2026-05-26 (task #3 per session) in response to Glific support
# ticket reply by Priyanshu (Glific) re. Himani-TAP escalation_order issue.
#
# Glific separates contact field VALUE from contact field DEFINITION:
#   - VALUE      → stored in contacts.fields JSON via `updateContact`.
#                  Visible in the contact profile JSON.
#   - DEFINITION → registered in the contacts_fields table via
#                  `createContactsField`. Required to make the field:
#                    (a) selectable in the Flow Editor variable dropdown,
#                    (b) resolvable via @contact.fields.<shortcode> in
#                        flow templates / send-message nodes,
#                    (c) returned in webhook query parameters where
#                        applicable.
#
# Without (b), template tokens like @contact.fields.bonus_quiz_points
# render as LITERAL TEXT to the end user — the root cause of the
# "Submission Missing!" garbled card on Himani's contact (2026-05-26).
#
# Idempotency: createContactsField returns an error with key 'shortcode'
# and message like 'has already been taken' when the field exists. The
# helper treats that as a no-op (returns True). Other errors return False.
#
# Run once per Glific organization (dev, prod) via the bootstrap function
# `dev_tools.bootstrap_sp_contact_fields()`. After that, the standard
# update_contact_fields path is sufficient because the definitions persist.

def register_contact_field(shortcode, display_name, value_type="TEXT",
                           scope="CONTACT"):
    """Register a Glific contact field DEFINITION (idempotent).

    Args:
        shortcode:   String — exact key used in @contact.fields.<shortcode>.
                     Must match the CF_* constant from constants.py.
        display_name: String — human-readable name shown in the Glific UI.
        value_type:  Glific enum — TEXT / NUMBER / DATE / etc. TEXT is the
                     safe default because Glific's flow rendering converts
                     numbers to strings anyway and the JSON we store via
                     updateContact uses {"type": "string"}.
        scope:       CONTACT (default — per-contact) or WA_GROUP / RELATIONSHIP.
                     We only use CONTACT.

    Returns:
        True  — field now exists (newly created OR already existed).
        False — registration failed for some other reason. Logged.

    Network: one POST to Glific GraphQL API. Synchronous.
    """
    settings = get_glific_settings()
    url = f"{settings.api_url}/api"

    payload = {
        "query": """
        mutation CreateContactsField($input: ContactsFieldInput!) {
          createContactsField(input: $input) {
            contactsField {
              id
              name
              shortcode
              valueType
              scope
            }
            errors {
              key
              message
            }
          }
        }
        """,
        "variables": {
            "input": {
                "name": display_name,
                "shortcode": shortcode,
                "valueType": value_type,
                "scope": scope,
            },
        },
    }

    try:
        # CR-025: 401-retry helper fetches headers internally
        response = _glific_post_with_401_retry(url, payload)
        data = response.json()

        if "errors" in data:
            frappe.logger().error(
                f"Glific register_contact_field GraphQL error "
                f"for shortcode={shortcode!r}: {data['errors']}"
            )
            return False

        result = data.get("data", {}).get("createContactsField", {})
        mutation_errors = result.get("errors") or []

        # Idempotency: treat "already taken" / "already exists" as success.
        if mutation_errors:
            for err in mutation_errors:
                msg = (err.get("message") or "").lower()
                if ("already" in msg) or ("taken" in msg) or ("exists" in msg):
                    # Already registered — no-op success.
                    return True
            # Some other mutation error (validation, permission, etc.)
            frappe.logger().error(
                f"Glific register_contact_field mutation error "
                f"for shortcode={shortcode!r}: {mutation_errors}"
            )
            return False

        if result.get("contactsField"):
            return True

        # Empty errors AND no contactsField — unexpected response shape.
        frappe.logger().error(
            f"Glific register_contact_field unexpected response "
            f"for shortcode={shortcode!r}: {data}"
        )
        return False

    except requests.exceptions.RequestException as e:
        frappe.logger().error(
            f"Glific register_contact_field network error "
            f"for shortcode={shortcode!r}: {e}"
        )
        return False
    except Exception as e:
        frappe.logger().error(
            f"Glific register_contact_field error "
            f"for shortcode={shortcode!r}: {e}"
        )
        return False


def get_contact_by_phone(phone):
    settings = get_glific_settings()
    url = f"{settings.api_url}/api"
    payload = {
        "query": """
        query contactByPhone($phone: String!) {
          contactByPhone(phone: $phone) {
            contact {
              id
              name
              optinTime
              optoutTime
              phone
              bspStatus
              status
              lastMessageAt
              fields
              settings
            }
          }
        }
        """,
        "variables": {
            "phone": phone
        }
    }

    try:
        # CR-025: 401-retry helper fetches headers internally
        response = _glific_post_with_401_retry(url, payload)
        data = response.json()

        if "errors" in data:
            frappe.logger().error(f"Glific API Error in getting contact by phone: {data['errors']}")
            return None

        contact = data.get("data", {}).get("contactByPhone", {}).get("contact")
        if contact:
            return contact
        else:
            frappe.logger().error(f"Contact not found for phone: {phone}")
            return None
    except requests.exceptions.RequestException as e:
        frappe.logger().error(f"Error calling Glific API to get contact by phone: {str(e)}")
        raise  # FIX 2: transient network errors must propagate so the retry/DLQ path fires

def optin_contact(phone, name):
    settings = get_glific_settings()
    url = f"{settings.api_url}/api"
    payload = {
        "query": """
        mutation optinContact($phone: String!, $name: String) {
          optinContact(phone: $phone, name: $name) {
            contact {
              id
              phone
              name
              lastMessageAt
              optinTime
              bspStatus
            }
            errors {
              key
              message
            }
          }
        }
        """,
        "variables": {
            "phone": phone,
            "name": name
        }
    }

    try:
        # CR-025: 401-retry helper fetches headers internally
        response = _glific_post_with_401_retry(url, payload)
        data = response.json()

        if "errors" in data:
            frappe.logger().error(f"Glific API Error in opting in contact: {data['errors']}")
            return False

        contact = data.get("data", {}).get("optinContact", {}).get("contact")
        if contact:
            frappe.logger().info(f"Contact opted in successfully: {contact}")
            return True
        else:
            frappe.logger().error(f"Failed to opt in contact. Response: {data}")
            return False
    except requests.exceptions.RequestException as e:
        frappe.logger().error(f"Error calling Glific API to opt in contact: {str(e)}")
        raise  # FIX 2: transient network errors must propagate

def create_contact_old(name, phone):
    settings = get_glific_settings()
    url = f"{settings.api_url}/api"
    payload = {
        "query": "mutation createContact($input:ContactInput!) { createContact(input: $input) { contact { id name phone } errors { key message } } }",
        "variables": {
            "input": {
                "name": name,
                "phone": phone
            }
        }
    }

    frappe.logger().info(f"Attempting to create Glific contact. Name: {name}, Phone: {phone}")
    frappe.logger().info(f"Glific API URL: {url}")
    frappe.logger().info(f"Glific API Payload: {payload}")

    try:
        # CR-025: 401-retry helper fetches headers internally
        response = _glific_post_with_401_retry(url, payload)
        frappe.logger().info(f"Glific API response status: {response.status_code}")
        frappe.logger().info(f"Glific API response content: {response.text}")

        if response.status_code == 200:
            data = response.json()
            if "errors" in data:
                frappe.logger().error(f"Error creating Glific contact: {data['errors']}")
                return None
            if "data" in data and "createContact" in data["data"] and "contact" in data["data"]["createContact"]:
                contact = data["data"]["createContact"]["contact"]
                frappe.logger().info(f"Glific contact created successfully: {contact}")
                return contact
            else:
                frappe.logger().error(f"Unexpected response structure: {data}")
                return None
        else:
            frappe.logger().error(f"Failed to create Glific contact. Status code: {response.status_code}")
            return None
    except Exception as e:
        frappe.logger().error(f"Exception occurred while creating Glific contact: {str(e)}", exc_info=True)
        return None

def start_contact_flow(flow_id, contact_id, default_results):
    settings = get_glific_settings()
    url = f"{settings.api_url}/api"
    payload = {
        "query": """
        mutation startContactFlow($flowId: ID!, $contactId: ID!, $defaultResults: Json!) {
            startContactFlow(flowId: $flowId, contactId: $contactId, defaultResults: $defaultResults) {
                success
                errors {
                    key
                    message
                }
            }
        }
        """,
        "variables": {
            "flowId": flow_id,
            "contactId": contact_id,
            "defaultResults": json.dumps(default_results)
        }
    }

    try:
        # CR-025: 401-retry helper fetches headers internally
        response = _glific_post_with_401_retry(url, payload)
        data = response.json()

        if "errors" in data:
            frappe.logger().error(f"{data}")
            frappe.logger().error(f"Glific API Error in starting flow: {data['errors']}")
            return False

        success = data.get("data", {}).get("startContactFlow", {}).get("success")
        if success:
            return True
        else:
            frappe.logger().error(f"Failed to start Glific flow. Response: {data}")
            return False
    except requests.exceptions.RequestException as e:
        # L-035: surface to the Error Log (operator-visible + picked up by the
        # hourly watchers), not just the bench log file. Contract preserved —
        # the 7 callers (pe_dispatcher, escalation, feedback, weekly flows,
        # onboarding) rely on the False return, so we log loudly and return
        # False rather than raising. A persistent 401 is handled INSIDE
        # _glific_post_with_401_retry (token invalidated before it raises), so
        # the next call recovers.
        try:
            frappe.log_error(
                f"start_contact_flow network error: flow={flow_id} "
                f"contact={contact_id}: {e}",
                "Glific start_contact_flow Error",
            )
        except Exception:
            frappe.logger().error(f"start_contact_flow error (double-fault): {e}")
        return False

def update_student_glific_ids(batch_size=100):
    def format_phone(phone):
        phone = phone.strip().replace(' ', '')
        if len(phone) == 10:
            return f"91{phone}"
        elif len(phone) == 12 and phone.startswith('91'):
            return phone
        else:
            return None

    students = frappe.get_all(
        "Student",
        filters={"glific_id": ["in", ["", None]]},
        fields=["name", "phone"],
        limit=batch_size
    )

    for student in students:
        formatted_phone = format_phone(student.phone)
        if not formatted_phone:
            frappe.logger().warning(f"Invalid phone number for student {student.name}: {student.phone}")
            continue

        glific_contact = get_contact_by_phone(formatted_phone)
        if glific_contact and 'id' in glific_contact:
            frappe.db.set_value("Student", student.name, "glific_id", glific_contact['id'])
            frappe.logger().info(f"Updated Glific ID for student {student.name}: {glific_contact['id']}")
        else:
            frappe.logger().warning(f"No Glific contact found for student {student.name} with phone {formatted_phone}")

    frappe.db.commit()
    return len(students)



def check_glific_group_exists(group_label):
    """Check if a group with the given label already exists in Glific"""
    settings = get_glific_settings()
    url = f"{settings.api_url}/api"

    payload = {
        "query": """
        query groups($filter: GroupFilter, $opts: Opts) {
          groups(filter: $filter, opts: $opts) {
            id
            label
          }
        }
        """,
        "variables": {
            "filter": {
                "label": group_label
            },
            "opts": {}
        }
    }

    try:
        # CR-025: 401-retry helper fetches headers internally
        response = _glific_post_with_401_retry(url, payload)
        data = response.json()

        if "errors" in data:
            frappe.logger().error(f"Glific API Error in checking group: {data['errors']}")
            return None

        groups = data.get("data", {}).get("groups", [])
        if groups:
            return groups[0]  # Return the first matching group
        return None
    except Exception as e:
        frappe.logger().error(f"Error checking Glific group: {str(e)}")
        return None

def create_glific_group(label, description=""):
    """Create a new group in Glific"""
    settings = get_glific_settings()
    url = f"{settings.api_url}/api"

    payload = {
        "query": """
        mutation createGroup($input: GroupInput!) {
          createGroup(input: $input) {
            group {
              id
              label
              description
            }
            errors {
              key
              message
            }
          }
        }
        """,
        "variables": {
            "input": {
                "label": label,
                "description": description
            }
        }
    }

    try:
        # CR-025: 401-retry helper fetches headers internally
        response = _glific_post_with_401_retry(url, payload)
        data = response.json()

        if "errors" in data:
            frappe.logger().error(f"Glific API Error in creating group: {data['errors']}")
            return None

        if "data" in data and "createGroup" in data["data"]:
            if "errors" in data["data"]["createGroup"] and data["data"]["createGroup"]["errors"]:
                errors = data["data"]["createGroup"]["errors"]
                frappe.logger().error(f"Glific API Error in creating group: {errors}")
                return None

            if "group" in data["data"]["createGroup"]:
                return data["data"]["createGroup"]["group"]

        frappe.logger().error(f"Unexpected response structure: {data}")
        return None
    except Exception as e:
        frappe.logger().error(f"Error creating Glific group: {str(e)}")
        return None

def create_or_get_glific_group_for_batch(set_id):
    """Create a Glific group for a backend onboarding batch or get existing one"""
    # Get the batch document
    set = frappe.get_doc("Backend Student Onboarding", set_id)

    # Check if we already have a mapping for this batch
    existing_mapping = frappe.get_all("GlificContactGroup",
                                   filters={"backend_onboarding_set": set_id},
                                   fields=["name", "group_id", "label"])

    if existing_mapping:
        return existing_mapping[0]

    # Derive group label from batch name
    group_label = f"Set: {set.set_name}"

    # Check if this group already exists in Glific
    existing_group = check_glific_group_exists(group_label)

    if existing_group:
        # Group exists, create mapping
        glific_group = frappe.new_doc("GlificContactGroup")
        glific_group.group_id = existing_group["id"]
        glific_group.label = existing_group["label"]
        glific_group.description = f"Auto-created for backend onboarding batch {set.set_name}"
        glific_group.backend_onboarding_set = set_id
        glific_group.insert()
        return {
            "group_id": existing_group["id"],
            "label": existing_group["label"]
        }

    # Group doesn't exist, create it in Glific
    new_group = create_glific_group(group_label, f"Students from batch {set.set_name}")

    if new_group:
        # Create mapping
        glific_group = frappe.new_doc("GlificContactGroup")
        glific_group.group_id = new_group["id"]
        glific_group.label = new_group["label"]
        glific_group.description = f"Auto-created for backend onboarding batch {set.set_name}"
        glific_group.backend_onboarding_set = set_id
        glific_group.insert()
        return {
            "group_id": new_group["id"],
            "label": new_group["label"]
        }

    # Failed to create group
    return None

def remove_contact_from_group(contact_id, group_id):
    """Remove a single contact from a single Glific group.

    CR-005 (2026-05-15): used by collection_membership state-driven writes.
    Wraps the same updateGroupContacts mutation as add_contact_to_group,
    routing the contact through `deleteContactIds` instead of
    `addContactIds`. Idempotent on the Glific side — removing a contact
    not in the group is a no-op.
    """
    if not contact_id or not group_id:
        return False

    settings = get_glific_settings()
    url = f"{settings.api_url}/api"

    payload = {
        "query": """
        mutation updateGroupContacts($input: GroupContactsInput!) {
          updateGroupContacts(input: $input) {
            groupContacts {
              id
            }
            numberDeleted
          }
        }
        """,
        "variables": {
            "input": {
                "groupId": group_id,
                "addContactIds": [],
                "deleteContactIds": [contact_id]
            }
        }
    }

    try:
        # CR-025: 401-retry helper fetches headers internally
        response = _glific_post_with_401_retry(url, payload)
        data = response.json()

        if "errors" in data:
            frappe.logger().error(
                f"Glific API Error removing contact from group: {data['errors']}"
            )
            return False

        if "data" in data and "updateGroupContacts" in data["data"]:
            if (
                "errors" in data["data"]["updateGroupContacts"]
                and data["data"]["updateGroupContacts"]["errors"]
            ):
                errors = data["data"]["updateGroupContacts"]["errors"]
                frappe.logger().error(
                    f"Glific API Error removing contact from group: {errors}"
                )
                return False
            return True

        return False
    except Exception as e:
        frappe.logger().error(f"Error removing contact from group: {str(e)}")
        return False


def create_group_if_missing(label, description=""):
    """Idempotent Glific group helper used by CR-005 collection bootstrap.

    Looks up `label` first; if a group with that label exists, returns its
    Glific group id. Otherwise creates the group and returns the new id.
    Returns None on API failure (caller decides whether to retry).

    Used by `activate_bpr` and the `backfill_pg_collection_kinds` patch to
    create the 5 kind-keyed collections per BPR without duplicating groups
    on re-runs.
    """
    existing = check_glific_group_exists(label)
    if existing and existing.get("id"):
        return existing["id"]

    new_group = create_glific_group(label, description)
    if new_group and new_group.get("id"):
        return new_group["id"]

    frappe.log_error(
        f"create_group_if_missing: failed to look up or create '{label}'",
        "Glific Group Bootstrap",
    )
    return None


def add_contact_to_group(contact_id, group_id):
    """Add a single contact to a single group"""
    if not contact_id or not group_id:
        return False

    settings = get_glific_settings()
    url = f"{settings.api_url}/api"

    payload = {
        "query": """
        mutation updateGroupContacts($input: GroupContactsInput!) {
          updateGroupContacts(input: $input) {
            groupContacts {
              id
            }
            numberDeleted
          }
        }
        """,
        "variables": {
            "input": {
                "groupId": group_id,
                "addContactIds": [contact_id],
                "deleteContactIds": []
            }
        }
    }

    try:
        # CR-025: 401-retry helper fetches headers internally
        response = _glific_post_with_401_retry(url, payload)
        data = response.json()

        if "errors" in data:
            frappe.logger().error(f"Glific API Error adding contact to group: {data['errors']}")
            return False

        if "data" in data and "updateGroupContacts" in data["data"]:
            if "errors" in data["data"]["updateGroupContacts"] and data["data"]["updateGroupContacts"]["errors"]:
                errors = data["data"]["updateGroupContacts"]["errors"]
                frappe.logger().error(f"Glific API Error adding contact to group: {errors}")
                return False

            return True

        return False
    except requests.exceptions.RequestException as e:
        frappe.logger().error(f"Network error adding contact to group: {str(e)}")
        raise  # FIX 2: transient network errors must propagate
    except Exception as e:
        frappe.logger().error(f"Error adding contact to group: {str(e)}")
        return False



def add_student_to_glific_for_onboarding(student_name, phone, school_name, batch_id, group_id, language_id=None, course_level_name=None, course_vertical_name=None, grade=None):
    """
    Function dedicated to backend onboarding that:
    1. Formats the phone number correctly
    2. Checks if contact already exists in Glific
    3. Creates contact if needed or just adds to group if exists
    4. ADDED: Opts in the contact for WhatsApp messaging
    
    Args:
        student_name: Name of the student
        phone: Phone number
        school_name: Name of the school
        batch_name: Name of the batch
        group_id: Glific group ID to add contact to
        language_id: Glific language ID from TAP Language
        course_level_name: Course level name for Glific
        course_vertical_name: Course vertical name for Glific
        grade: Student grade for Glific
        
    Returns:
        Contact information if successful, None otherwise
    """
    settings = get_glific_settings()

    # Format phone number
    phone = phone.strip().replace(' ', '')
    if len(phone) == 10:
        phone = f"91{phone}"
    elif len(phone) == 12 and phone.startswith('91'):
        pass  # Phone is already properly formatted
    else:
        frappe.logger().warning(f"Invalid phone number format: {phone}")
        return None

    # Check if contact already exists
    existing_contact = get_contact_by_phone(phone)

    if existing_contact and 'id' in existing_contact:
        frappe.logger().info(f"Contact already exists in Glific. Using existing contact: {existing_contact['id']}")
        
        # ADDED: Check if contact is opted in and opt-in if needed
        bsp_status = existing_contact.get('bspStatus', 'NONE')
        if bsp_status not in ['SESSION', 'SESSION_AND_HSM']:
            frappe.logger().info(f"Existing contact not opted in. Attempting opt-in...")
            try:
                optin_result = optin_contact(phone, student_name)
                if optin_result:
                    frappe.logger().info(f"Successfully opted in contact: {phone}")
                else:
                    frappe.logger().warning(f"Failed to opt-in contact: {phone}")
            except Exception as e:
                frappe.logger().warning(f"Error during opt-in: {str(e)}")
                # Continue even if opt-in fails

        # Add to group
        if group_id:
            add_contact_to_group(existing_contact['id'], group_id)

        # Optionally update fields to ensure they're current
        fields_to_update = {
            "buddy_name": student_name,
            "batch_id": batch_id
        }
        if school_name:
            fields_to_update["school"] = school_name
        if course_level_name:
            fields_to_update["course_level"] = course_level_name
        if course_vertical_name:
            fields_to_update["course"] = course_vertical_name
        if grade:
            fields_to_update["grade"] = grade

        # 2026-05-19 — defensive existing-contact branch should also update
        # the CORE language. Reached when process_glific_contact's initial
        # lookup missed a contact (race / late-creation) but this function
        # re-found one. Pass language_id so it's set in the same mutation
        # as the field updates — parity with process_glific_contact's main
        # existing-contact path.
        update_contact_fields(
            existing_contact['id'],
            fields_to_update,
            language_id=language_id,
        )

        return existing_contact
    else:
        # Get language_id from the parameter or use default if not provided
        if language_id is None or language_id == "":
            # Try to get default language ID from Glific Settings
            try:
                language_id = frappe.db.get_single_value("Glific Settings", "default_language_id")
            except Exception as e:
                frappe.logger().warning(f"Error getting default_language_id: {str(e)}")
                language_id = "1"  # Default to English if not found
        
        # Ensure language_id is an integer
        try:
            language_id = int(language_id)
        except (ValueError, TypeError):
            frappe.logger().warning(f"Invalid language_id format: {language_id}, using default (1)")
            language_id = 1  # Default to English if not a valid integer
        
        frappe.logger().info(f"Creating Glific contact with language_id: {language_id}")

        # Create new contact with minimal required fields
        contact_data = {
            "query": """
            mutation createContact($input:ContactInput!) {
                createContact(input: $input) {
                    contact { id name phone }
                    errors { key message }
                }
            }
            """,
            "variables": {
                "input": {
                    "name": student_name,
                    "phone": phone,
                    "languageId": language_id
                }
            }
        }

        # Add fields if available
        fields = {}
        # Always add buddy_name
        fields["buddy_name"] = {
            "value": student_name,
            "type": "string",
            "inserted_at": datetime.now(timezone.utc).isoformat()
        }
        
        if school_name:
            fields["school"] = {
                "value": school_name,
                "type": "string",
                "inserted_at": datetime.now(timezone.utc).isoformat()
            }

        if batch_id:
            fields["batch_id"] = {
                "value": batch_id,
                "type": "string",
                "inserted_at": datetime.now(timezone.utc).isoformat()
            }

        if course_level_name:
            fields["course_level"] = {
                "value": course_level_name,
                "type": "string",
                "label": "course_level",
                "inserted_at": datetime.now(timezone.utc).isoformat()
            }

        if course_vertical_name:
            fields["course"] = {
                "value": course_vertical_name,
                "type": "string",
                "inserted_at": datetime.now(timezone.utc).isoformat()
            }

        if grade:
            fields["grade"] = {
                "value": grade,
                "type": "string",
                "inserted_at": datetime.now(timezone.utc).isoformat()
            }

        if fields:
            contact_data["variables"]["input"]["fields"] = json.dumps(fields)

        # Execute request
        try:
            # CR-025: 401-retry helper fetches headers internally
            response = _glific_post_with_401_retry(
                f"{settings.api_url}/api",
                contact_data,
            )

            if response.status_code != 200:
                frappe.logger().error(f"Failed to create contact. Status: {response.status_code}, Response: {response.text}")
                return None

            result = response.json()

            if "errors" in result:
                frappe.logger().error(f"GraphQL errors: {result['errors']}")
                return None

            contact = result.get("data", {}).get("createContact", {}).get("contact")

            if not contact:
                frappe.logger().error(f"No contact in response: {result}")
                return None

            # ADDED: Opt-in the newly created contact
            frappe.logger().info(f"Contact created. Now attempting opt-in...")
            try:
                optin_result = optin_contact(phone, student_name)
                if optin_result:
                    frappe.logger().info(f"Successfully opted in new contact: {phone}")
                else:
                    frappe.logger().warning(f"Failed to opt-in new contact: {phone}")
            except Exception as e:
                frappe.logger().warning(f"Error during opt-in for new contact: {str(e)}")
                # Continue even if opt-in fails

            # Add to group
            if group_id and 'id' in contact:
                add_contact_to_group(contact['id'], group_id)

            return contact

        except requests.exceptions.RequestException as e:
            frappe.logger().error(f"Network error in add_student_to_glific_for_onboarding: {str(e)}", exc_info=True)
            raise  # FIX 2: transient network errors must propagate
        except Exception as e:
            frappe.logger().error(f"Exception in add_student_to_glific_for_onboarding: {str(e)}", exc_info=True)
            return None




def create_or_get_teacher_group_for_batch(batch_name, batch_id):
    """
    Create a Glific group for teachers in a batch or get existing one

    Args:
        batch_name: The Batch document name (link field)
        batch_id: The batch_id field value from the Batch document
    """

    # Handle edge case for no active batch
    if not batch_id or batch_id == "no_active_batch_id" or not batch_name:
        frappe.logger().warning(f"Invalid batch for teacher group: batch_name={batch_name}, batch_id={batch_id}")
        return None

    # Check if we already have a mapping for this batch document
    existing_mapping = frappe.get_all("Glific Teacher Group",
                                   filters={"batch": batch_name},
                                   fields=["name", "glific_group_id", "group_label"])

    if existing_mapping:
        frappe.logger().info(f"Found existing teacher group mapping for batch {batch_name}")
        return {
            "group_id": existing_mapping[0]["glific_group_id"],
            "label": existing_mapping[0]["group_label"]
        }

    # Derive group label from batch_id
    group_label = f"teacher_batch_{batch_id}"

    # Check if this group already exists in Glific
    existing_group = check_glific_group_exists(group_label)

    if existing_group:
        # Group exists in Glific, create mapping
        frappe.logger().info(f"Found existing Glific group: {existing_group}")

        teacher_group = frappe.new_doc("Glific Teacher Group")
        teacher_group.batch = batch_name
        teacher_group.batch_id = batch_id
        teacher_group.glific_group_id = existing_group["id"]
        teacher_group.group_label = existing_group["label"]
        teacher_group.description = f"Teachers from batch {batch_id}"
        teacher_group.created_date = frappe.utils.now_datetime()
        teacher_group.insert(ignore_permissions=True)
        frappe.db.commit()

        return {
            "group_id": existing_group["id"],
            "label": existing_group["label"]
        }

    # Group doesn't exist, create it in Glific
    frappe.logger().info(f"Creating new Glific group for teacher batch {batch_id}")
    new_group = create_glific_group(group_label, f"Teachers from batch {batch_id}")

    if new_group:
        # Create mapping
        teacher_group = frappe.new_doc("Glific Teacher Group")
        teacher_group.batch = batch_name
        teacher_group.batch_id = batch_id
        teacher_group.glific_group_id = new_group["id"]
        teacher_group.group_label = new_group["label"]
        teacher_group.description = f"Teachers from batch {batch_id}"
        teacher_group.created_date = frappe.utils.now_datetime()
        teacher_group.insert(ignore_permissions=True)
        frappe.db.commit()

        return {
            "group_id": new_group["id"],
            "label": new_group["label"]
        }

    # Failed to create group
    frappe.logger().error(f"Failed to create Glific group for batch {batch_id}")
    return None


# ── CR-025 Layer 3a — hourly token-health probe ────────────────────────────────
# Registered as an hourly cron in hooks.py (requires `bench migrate` to land
# per L-049).
#
# Sends a lightweight Glific API call (the "me" query) to verify the token
# stored in Glific Settings is valid. If the probe returns 401, the stored
# token is invalidated so the next real API call triggers a fresh login.
#
# This is a PREVENTIVE probe — it doesn't block the incident (that's handled
# by _glific_post_with_401_retry on each API call). Its value is surfacing
# silent token drift earlier and giving the hourly alert dashboard a signal.
#
# IMPORTANT: the probe makes ONE Glific API call on success path (200) and
# ONE on 401 path (then token invalidated). It NEVER calls the auth endpoint
# (/api/v1/session) itself — _invalidate_stored_token just clears the DB value
# so the next get_glific_auth_headers() call does the fresh login.

def probe_token_health():
    """Hourly cron: verify the stored Glific token is still valid.

    Makes a lightweight introspection query against the Glific /api endpoint.
    On 401: invalidates the stored token (so the next real API call triggers
    a fresh login). On 200: no-op. On other errors: logs and returns without
    invalidating (connection errors don't indicate an auth problem).

    Scheduled in hooks.py under the existing "0 * * * *" hourly block.
    Requires `bench --site <site> migrate` to register the scheduler entry.
    """
    settings = get_glific_settings()
    url = f"{settings.api_url}/api"

    # The lightest possible query — just reads the current user's name.
    probe_payload = {
        "query": "{ currentUser { user { name } } }"
    }

    try:
        headers = get_glific_auth_headers()
        resp = _GLIFIC_SESSION.post(url, json=probe_payload, headers=headers,
                                    timeout=GLIFIC_TIMEOUT)

        if resp.status_code == 401:
            frappe.logger().warning(
                "probe_token_health: 401 from Glific — invalidating cached token "
                "so next API call triggers fresh login."
            )
            _invalidate_stored_token()
            frappe.log_error(
                "probe_token_health detected stale Glific token (HTTP 401). "
                "Token has been invalidated; next API call will re-authenticate. "
                "If this fires repeatedly, check Glific credentials in Glific Settings.",
                "Glific Token Health Alert",
            )
        elif resp.ok:
            frappe.logger().debug(
                f"probe_token_health: token OK (HTTP {resp.status_code})"
            )
        else:
            frappe.logger().warning(
                f"probe_token_health: unexpected HTTP {resp.status_code} — "
                f"not a 401, so token not invalidated. Body: {resp.text[:200]}"
            )

    except Exception as exc:
        # Connection errors (timeout, DNS) don't indicate a bad token.
        # Log the connectivity problem but don't invalidate — a valid token
        # is better than no token when Glific comes back.
        frappe.logger().error(
            f"probe_token_health: connectivity error (not invalidating token): {exc}"
        )
