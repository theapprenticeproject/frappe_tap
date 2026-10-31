import frappe
from frappe.query_builder import DocType


def execute():
    submission = DocType("Submission")
    assignment = DocType("Assignment")
    (
        frappe.qb.update(submission)
        .set(submission.assignment_id_link, submission.assign_id)
        .where(submission.assign_id.isin(frappe.qb.from_(assignment).select(assignment.name)))
        .run()
    )
