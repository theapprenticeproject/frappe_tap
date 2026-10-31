import frappe


def execute():
    for doctype in ("StudentQuizAttempt", "StudentStageProgress"):
        frappe.delete_doc("DocType", doctype, force=True)
