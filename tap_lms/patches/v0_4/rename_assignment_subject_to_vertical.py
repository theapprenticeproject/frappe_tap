import frappe
from frappe.query_builder import DocType


def execute():
    frappe.reload_doc("tap_lms", "doctype", "assignment")
    assignment = DocType("Assignment")
    frappe.qb.update(assignment).set(assignment.vertical, assignment.subject).run()
