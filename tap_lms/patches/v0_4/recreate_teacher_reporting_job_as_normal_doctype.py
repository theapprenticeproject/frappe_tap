import frappe


def execute():
    frappe.delete_doc("DocType", "Teacher Reporting Job", force=True)
