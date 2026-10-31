import re

import frappe


def execute():
    """Rename Assignments named '{assignment_name}-{###}' back to '{assignment_name}-{difficulty_tier}'."""
    for name, assignment_name, tier in frappe.get_all(
        "Assignment", fields=["name", "assignment_name", "difficulty_tier"], as_list=True
    ):
        target = f"{assignment_name}-{tier}"
        if not re.fullmatch(re.escape(f"{assignment_name}-") + r"\d{3}", name):
            continue
        if frappe.db.exists("Assignment", target):
            print(f"Skipped {name}: {target} already exists")
            continue
        frappe.rename_doc("Assignment", name, target, force=True, rebuild_search=False)
