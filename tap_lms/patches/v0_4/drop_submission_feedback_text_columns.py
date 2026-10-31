import frappe

COLUMNS = ("learning_objectives_feedback", "strengths", "areas_for_improvement", "encouragement")


def execute():
    """Drop the columns of the fields removed from Submission."""
    for column in COLUMNS:
        if frappe.db.has_column("Submission", column):
            frappe.db.sql_ddl(f"ALTER TABLE `tabSubmission` DROP COLUMN `{column}`")
