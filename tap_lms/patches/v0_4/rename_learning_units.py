import frappe
from frappe.model.naming import getseries
from frappe.query_builder import DocType


def execute():
    """Rename Learning Units to 'Unit -{week_no}-{course_level}-{###}', using the
    week number on the Course Level's learning_units row."""
    unit_list = DocType("LearningUnitList")
    rows = (
        frappe.qb.from_(unit_list)
        .select(unit_list.learning_unit, unit_list.parent, unit_list.week_no)
        .where(unit_list.parenttype == "Course Level")
        .orderby(unit_list.parent)
        .orderby(unit_list.idx)
        .run()
    )

    renamed = set()
    for unit, course_level, week_no in rows:
        if unit in renamed:
            continue
        prefix = f"Unit -{week_no}-{course_level}-"
        frappe.rename_doc("LearningUnit", unit, prefix + getseries(prefix, 3), force=True, rebuild_search=False)
        renamed.add(unit)
