import frappe
import random
import string


def generate_unique_keyword(name1):
    words = name1.split()
    first_two_letters = "".join([word[:2].upper() for word in words if word])
    random_number = random.randint(10, 99)
    random_letters = ''.join(random.choices(string.ascii_uppercase, k=3))
    return first_two_letters + str(random_number) + random_letters


def get_school_state_model_details(school_id):
    if not school_id:
        return {
            "school_name": "",
            "state_id": "",
            "state_name": "",
            "model_link": "",
            "model_name": "",
        }

    school = frappe.db.get_value(
        "School",
        school_id,
        ["name1", "state", "model"],
        as_dict=True,
    ) or {}

    state_id = school.get("state")
    state = (
        frappe.db.get_value("State", state_id, ["state_name"], as_dict=True)
        if state_id
        else {}
    ) or {}

    model_link = school.get("model")
    model_name = (
        frappe.db.get_value("Tap Models", model_link, "mname")
        if model_link
        else ""
    ) or ""

    return {
        "school_name": school.get("name1") or "",
        "state_id": state_id or "",
        "state_name": state.get("state_name") or "",
        "model_link": model_link or "",
        "model_name": model_name,
    }
