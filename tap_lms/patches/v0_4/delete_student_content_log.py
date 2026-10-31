import frappe


def execute():
    frappe.delete_doc("DocType", "StudentContentLog", force=True)
