import frappe


def execute():
    for doctype in ("StudentQuizAnswer", "StudentOnboardingProgress"):
        frappe.delete_doc("DocType", doctype, force=True)
