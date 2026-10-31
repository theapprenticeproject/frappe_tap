import frappe
from frappe.query_builder import DocType


def execute():
    """Set Assignment.assignment_type from the activity type of its Learning Unit
    (Assignment -> VideoClass -> LearningUnit)."""
    assessment = DocType("AssessmentList")
    content_item = DocType("UnitContentItem")
    unit = DocType("LearningUnit")

    rows = (
        frappe.qb.from_(assessment)
        .join(content_item)
        .on(
            (content_item.content == assessment.parent)
            & (content_item.content_type == "VideoClass")
            & (content_item.parenttype == "LearningUnit")
        )
        .join(unit)
        .on(unit.name == content_item.parent)
        .select(assessment.assessment, unit.activity_type)
        .where(
            (assessment.assessment_type == "Assignment")
            & (assessment.parenttype == "VideoClass")
            & unit.activity_type.isnotnull()
            & (unit.activity_type != "")
        )
        .run()
    )
    for assignment, activity_type in rows:
        frappe.db.set_value("Assignment", assignment, "assignment_type", activity_type.title(), update_modified=False)
