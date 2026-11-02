import frappe


def execute():
    """Write the field defaults of Feedback Delivery Settings to the database.

    A single doctype that has never been saved has no stored values; saving it
    once persists the defaults declared on the doctype fields.
    """
    frappe.reload_doc("tap_lms", "doctype", "feedback_delivery_settings")
    frappe.get_doc("Feedback Delivery Settings").save(ignore_permissions=True)
