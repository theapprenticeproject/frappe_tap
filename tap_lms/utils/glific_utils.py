import frappe
from frappe.utils.background_jobs import enqueue
from .glific_integration import update_student_glific_ids

@frappe.whitelist()
def run_glific_id_update():
    total_students = frappe.db.count("Student", {"glific_id": ["in", ["", None]]})
    if total_students == 0:
        return "No students found without Glific ID."

    job = enqueue(
        process_glific_id_update,
        queue='long',
        timeout=3600,
        total_students=total_students
    )
    return f"Glific ID update process started. Job ID: {job.id}"

def process_glific_id_update(total_students):
    batch_size = 100
    processed = 0

    while processed < total_students:
        updated = update_student_glific_ids(batch_size)
        processed += updated
        frappe.db.commit()
        frappe.publish_realtime("glific_id_update_progress", {"processed": processed, "total": total_students})

    frappe.publish_realtime("glific_id_update_complete", {"total_updated": processed})
