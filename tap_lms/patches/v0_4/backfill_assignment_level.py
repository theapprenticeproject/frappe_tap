import frappe
from frappe.query_builder import DocType


def execute():
    """Set Assignment.level from Course Level.level via
    Assignment -> VideoClass -> LearningUnit -> Course Level."""
    assessment = DocType("AssessmentList")
    content_item = DocType("UnitContentItem")
    unit_list = DocType("LearningUnitList")
    course_level = DocType("Course Level")

    rows = (
        frappe.qb.from_(assessment)
        .join(content_item)
        .on(
            (content_item.content == assessment.parent)
            & (content_item.content_type == "VideoClass")
            & (content_item.parenttype == "LearningUnit")
        )
        .join(unit_list)
        .on((unit_list.learning_unit == content_item.parent) & (unit_list.parenttype == "Course Level"))
        .join(course_level)
        .on(course_level.name == unit_list.parent)
        .select(assessment.assessment, course_level.level)
        .where((assessment.assessment_type == "Assignment") & (assessment.parenttype == "VideoClass"))
        .run()
    )
    for assignment, level in rows:
        frappe.db.set_value("Assignment", assignment, "level", level, update_modified=False)
