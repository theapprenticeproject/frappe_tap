import frappe
from frappe import _
import json
import requests
from .glific_integration import (
    get_glific_auth_headers,
    get_glific_settings,
    _glific_post_with_401_retry,
)

@frappe.whitelist()
def update_glific_contact(doc, method):
    if doc.doctype != "Teacher":
        return

    try:
        # Fetch Glific contact details
        glific_contact = get_glific_contact(doc.glific_id)
        if not glific_contact:
            frappe.logger().error(f"Glific contact not found for teacher {doc.name}")
            return

        # Prepare update payload
        update_payload = prepare_update_payload(doc, glific_contact)
        if not update_payload:
            frappe.logger().info(f"No updates needed for Glific contact {doc.glific_id}")
            return

        # Send update to Glific
        success = send_glific_update(doc.glific_id, update_payload)
        if success:
            frappe.logger().info(f"Successfully updated Glific contact for teacher {doc.name}")
        else:
            frappe.logger().error(f"Failed to update Glific contact for teacher {doc.name}")

    except Exception as e:
        frappe.logger().error(f"Error updating Glific contact for teacher {doc.name}: {str(e)}")

def get_glific_contact(glific_id):
    settings = get_glific_settings()
    url = f"{settings.api_url}/api"

    query = """
    query contact($id: ID!) {
      contact(id: $id) {
        contact {
          id
          name
          language {
            id
            label
          }
          fields
        }
      }
    }
    """

    payload = {"query": query, "variables": {"id": glific_id}}

    try:
        # CR-025: use 401-retry helper (was bare requests.post, no session, no timeout)
        response = _glific_post_with_401_retry(url, payload)
        data = response.json()
        return data.get("data", {}).get("contact", {}).get("contact")
    except Exception:
        return None





def prepare_update_payload(doc, glific_contact):
    field_mappings = frappe.get_all(
        "Glific Field Mapping",
        filters={"frappe_doctype": "Teacher"},
        fields=["frappe_field", "glific_field"]
    )

    current_fields = json.loads(glific_contact.get("fields", "{}"))
    update_fields = current_fields.copy()  # Start with all existing fields

    has_updates = False

    for mapping in field_mappings:
        frappe_value = doc.get(mapping.frappe_field)
        glific_field = mapping.glific_field
        
        if glific_field in current_fields:
            if frappe_value != current_fields[glific_field].get("value"):
                update_fields[glific_field] = {
                    "value": frappe_value,
                    "type": "string",
                    "inserted_at": frappe.utils.now_datetime().isoformat()
                }
                has_updates = True
        else:
            update_fields[glific_field] = {
                "value": frappe_value,
                "type": "string",
                "inserted_at": frappe.utils.now_datetime().isoformat()
            }
            has_updates = True

    # Handle language change
    frappe_language = doc.get("language")
    glific_language_id = frappe.db.get_value("TAP Language", {"language_name": frappe_language}, "glific_language_id")
    
    payload = {}
    
    if glific_language_id and int(glific_language_id) != int(glific_contact["language"]["id"]):
        payload["languageId"] = int(glific_language_id)
        has_updates = True

    if has_updates:
        payload["fields"] = json.dumps(update_fields)

    return payload if has_updates else None






def send_glific_update(glific_id, update_payload):
    settings = get_glific_settings()
    url = f"{settings.api_url}/api"

    query = """
    mutation updateContact($id: ID!, $input: ContactInput!) {
      updateContact(id: $id, input: $input) {
        contact {
          id
          fields
          language {
            label
          }
        }
        errors {
          key
          message
        }
      }
    }
    """

    variables = {
        "id": glific_id,
        "input": update_payload
    }

    try:
        # CR-025: use 401-retry helper (was bare requests.post, no session, no timeout)
        response = _glific_post_with_401_retry(url, {"query": query, "variables": variables})
        data = response.json()
        if "errors" in data:
            frappe.logger().error(f"Glific API Error: {data['errors']}")
            return False
        return True
    except Exception:
        return False
