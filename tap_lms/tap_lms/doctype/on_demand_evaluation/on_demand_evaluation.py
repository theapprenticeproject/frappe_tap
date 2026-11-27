# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

import re
from io import BytesIO
from itertools import cycle

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.workbook.properties import CalcProperties
from openpyxl.worksheet.datavalidation import DataValidation

from tap_lms.imgana.submission import enqueue_submissions_batch
from tap_lms.utils.google_drive_helper import (
	get_google_service_account_info,
	upload_bytes_to_google_drive,
)
from tap_lms.utils.google_sheet_helper import GoogleSheetsClient, parse_sheet_ref
from tap_lms.utils.scheduled_jobs import get_job_config

# Built by scripts/private/list_prod_learning_unit_names_to_google_sheet.py:
# one tab per "<vertical> <level>".
SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets/1nSN72ikEqM7U0gMl-3AoOV8KeHAzNZvK_zHCNcT6B34"
ON_DEMAND_EVALUATION_DRIVE_FOLDER_ID = "1ofBHmDSzeqImZpn9F5cHGJR3efDTqc5B"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ILLEGAL_XLSX_CHARACTERS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
RUBRIC_GRADES = {"Invalid": -1, "Novice": 1, "Beginner": 2, "Emerging": 3, "Proficient": 4, "Expert": 5}
LEVEL_RE = re.compile(r"\b(?:level|l)\s*(\d+[a-z]{0,2})\b", re.I)


class OnDemandEvaluation(Document):
	pass


@frappe.whitelist()
def queue_evaluations(docname):
	if frappe.db.get_value("On Demand Evaluation", docname, "status") != "Draft":
		frappe.throw("Only a Draft job can be queued.")
	frappe.enqueue(
		"tap_lms.tap_lms.doctype.on_demand_evaluation.on_demand_evaluation.run_queue_evaluations",
		queue="long",
		timeout=14400,
		docname=docname,
	)


def run_queue_evaluations(docname):
	doc = frappe.get_doc("On Demand Evaluation", docname)
	assignment_ids = _get_assignment_ids(doc)
	if not assignment_ids:
		frappe.throw(f"No assignments found in the sheet for {doc.program} / {doc.unit_activity_no}.")

	submissions = _get_submissions(doc, assignment_ids)
	if not submissions:
		frappe.throw(f"No submissions found for batch {doc.batch} and assignments {sorted(set(assignment_ids))}.")

	# Commit the job link before publishing so results can never arrive ahead of it.
	frappe.db.sql(
		"""UPDATE `tabSubmission` SET rubric_eval_status = 'Processing', rubric_eval_job = %s
		WHERE name IN %s""",
		(docname, tuple(submissions)),
	)
	doc.update({"status": "Queued", "total_assignments": len(assignment_ids), "total_submissions": len(submissions), "queued_at": now_datetime()})
	doc.save()
	frappe.db.commit()

	for i in range(0, len(submissions), 50):
		enqueue_submissions_batch(submissions[i : i + 50], eval_type="rubric")


@frappe.whitelist()
def count_evaluations(docname):
	doc = frappe.get_doc("On Demand Evaluation", docname)
	assignment_ids = _get_assignment_ids(doc)
	counts = {
		"total_assignments": len(assignment_ids),
		"total_submissions": len(_get_submissions(doc, assignment_ids)),
	}
	doc.update(counts)
	doc.save()
	return counts


def _get_submissions(doc, assignment_ids):
	if not assignment_ids:
		return []
	submissions = frappe.db.sql_list(
		"""
		SELECT s.name FROM `tabSubmission` s
		WHERE s.assign_id IN %(assignment_ids)s
		  AND IFNULL(s.submission_type, '') NOT IN ('text', 'emoji')
		  AND (IFNULL(s.submission_text, '') != '' OR IFNULL(s.submission_url, '') != '')
		  AND EXISTS (
			SELECT 1 FROM `tabStudent Enrollment` e
			WHERE e.parenttype = 'Student' AND e.parent = s.student_id AND e.batch = %(batch)s
		  )
		""",
		{"assignment_ids": tuple(assignment_ids), "batch": doc.batch},
	)
	return submissions[: doc.test_limit] if doc.test_limit else submissions


def _get_assignment_ids(doc):
	"""Return the sheet's assignment IDs and list them in doc.assignments (unsaved)."""
	verticals = [row.vertical for row in doc.verticals] or (
		frappe.get_meta("On Demand Evaluation Vertical").get_field("vertical").options.split("\n")
	)
	spreadsheet_id = SHEETS_URL.rsplit("/", 1)[-1]
	sheets = GoogleSheetsClient(get_google_service_account_info())
	titles = sheets.get_tab_titles(spreadsheet_id)
	# Tabs are named "<vertical> <level>".
	tabs = [(title, vertical) for title in titles for vertical in verticals if title.startswith(f"{vertical} ")]
	if not tabs:
		frappe.throw(f"No sheet tabs found for verticals {verticals}.")

	ranges = ["'{}'".format(title.replace("'", "''")) for title, _ in tabs]
	data = sheets.get_batch_values(spreadsheet_id, ranges)

	unit_activity_no = _normalize(doc.unit_activity_no)
	program = _normalize(doc.program)
	assignment_ids = []
	doc.set("assignments", [])
	for (title, vertical), value_range in zip(tabs, data):
		header, *rows = value_range.get("values") or [[]]
		unit_col = header.index("Unit & Activity No.")
		program_col = header.index("Program")
		assignment_col = header.index("Assignment")
		for row in rows:
			row = row + [""] * (len(header) - len(row))
			if _normalize(row[unit_col]) == unit_activity_no and _normalize(row[program_col]) == program:
				for assignment in (a.strip() for a in row[assignment_col].split(",") if a.strip()):
					assignment_ids.append(assignment)
					doc.append("assignments", {
						"assignment_name": assignment,
						"level": title[len(vertical) + 1 :],
						"vertical": vertical,
						"program": row[program_col],
					})

	# Leave the Assignment link blank for IDs missing in Frappe so they stand out.
	existing = set(frappe.get_all("Assignment", filters={"name": ["in", assignment_ids]}, pluck="name"))
	with_rubrics = set(frappe.get_all(
		"Rubric Grade", filters={"parenttype": "Assignment", "parent": ["in", assignment_ids]}, pluck="parent",
	))
	for row in doc.assignments:
		if row.assignment_name in existing:
			row.assignment = row.assignment_name
		row.rubric_present = row.assignment_name in with_rubrics
	return assignment_ids


@frappe.whitelist()
def fill_rubrics(docname):
	"""Fill Assessment Rubrics on the job's assignments from each vertical's rubric sheet (one tab per level)."""
	doc = frappe.get_doc("On Demand Evaluation", docname)
	if not doc.rubric_sheets:
		frappe.throw("Add a rubric sheet for at least one vertical.")
	_get_assignment_ids(doc)
	sheets = GoogleSheetsClient(get_google_service_account_info())
	for sheet in doc.rubric_sheets:
		errors = []
		try:
			spreadsheet_id = parse_sheet_ref(sheet.sheet_url).spreadsheet_id
			titles = sheets.get_tab_titles(spreadsheet_id, skip_hidden=True)
			for row in doc.assignments:
				if row.vertical != sheet.vertical or not row.assignment:
					continue
				try:
					assignment = frappe.get_doc("Assignment", row.assignment)
					if assignment.rubric_grades and not doc.replace_existing_rubrics:
						continue
					tabs = [title for title in titles if _tab_level(title) == row.level]
					if len(tabs) != 1:
						raise ValueError(f"expected 1 tab for {row.level} but found {len(tabs)}: {tabs}")
					assignment.set("rubric_grades", _parse_rubrics(sheets.get_values(spreadsheet_id, tabs[0])))
					assignment.save()
					row.rubric_present = 1
				except Exception as e:
					errors.append(f"{row.assignment}: {e}")
		except Exception as e:
			errors.append(str(e))
		sheet.error = "\n".join(errors)
	doc.save()


def _tab_level(title):
	match = LEVEL_RE.search(title)
	return f"Level {match.group(1).upper()}" if match else None


def _parse_rubrics(values):
	"""Rows of a rubric tab -> Rubric Grade rows. The header row holds the grade names and an optional Criteria column."""
	header_idx = next(
		(i for i, row in enumerate(values) if {c.strip().lower() for c in row} & {g.lower() for g in RUBRIC_GRADES}),
		None,
	)
	if header_idx is None:
		raise ValueError(f"no header row containing grade names {list(RUBRIC_GRADES)}")
	header = [c.strip().lower() for c in values[header_idx]]
	missing = [g for g in RUBRIC_GRADES if g.lower() not in header]
	if missing:
		raise ValueError(f"header row is missing grade columns {missing}")
	criteria_col = header.index("criteria") if "criteria" in header else 0

	rubrics = []
	for row in values[header_idx + 1 :]:
		row = [c.strip() for c in row] + [""] * (len(header) - len(row))
		if not row[criteria_col]:
			continue
		for grade, value in RUBRIC_GRADES.items():
			description = row[header.index(grade.lower())]
			if description:
				rubrics.append({
					"skill_name": row[criteria_col],
					"grade_name": grade,
					"grade_value": value,
					"grade_description": description,
				})
	return rubrics


def _normalize(value):
	return " ".join(str(value).split()).lower()


def check_queued_evaluations():
	"""Cron: complete Queued jobs once none of their submissions is still Processing."""
	if not get_job_config("on_demand_evaluation.check_queued_evaluations").enabled:
		return

	for name in frappe.get_all("On Demand Evaluation", filters={"status": "Queued"}, pluck="name"):
		if not frappe.db.exists("Submission", {"rubric_eval_job": name, "rubric_eval_status": "Processing"}):
			# The cron runs on the default queue (300s timeout); building the output can take longer.
			frappe.enqueue(
				"tap_lms.tap_lms.doctype.on_demand_evaluation.on_demand_evaluation.complete_evaluation",
				queue="long",
				timeout=3600,
				docname=name,
			)


def complete_evaluation(docname):
	doc = frappe.get_doc("On Demand Evaluation", docname)
	completed_at = now_datetime()
	file_name = _build_output_file_name(doc.program, doc.unit_activity_no, completed_at)
	url = upload_bytes_to_google_drive(
		_build_xlsx(docname),
		file_name,
		ON_DEMAND_EVALUATION_DRIVE_FOLDER_ID,
		XLSX_CONTENT_TYPE,
	)
	frappe.db.set_value("On Demand Evaluation", docname, {
		"status": "Completed",
		"completed_at": completed_at,
		"output_file_url": url,
	})
	frappe.db.commit()


def _build_output_file_name(program, unit_activity_no, timestamp):
	def clean(value):
		return re.sub(r"[/\x00-\x1f]+", "_", str(value or "").strip()).strip(" _")

	return f"{clean(program)}_{clean(unit_activity_no)}_{timestamp.strftime('%Y%m%d_%H%M%S')}.xlsx"


def _build_xlsx(job_name):
	batch = frappe.db.get_value("On Demand Evaluation", job_name, "batch")
	submissions = frappe.db.sql(
		"""
		SELECT s.name, s.student_id, s.submission_type, s.submission_url, s.assign_id, s.created_at,
			s.plagiarism_status, s.overall_feedback, s.overall_feedback_translated,
			a.vertical, a.level, school.state
		FROM `tabSubmission` s
		LEFT JOIN `tabAssignment` a ON a.name = s.assign_id
		LEFT JOIN `tabStudent Enrollment` enrollment ON enrollment.name = (
			SELECT e.name FROM `tabStudent Enrollment` e
			WHERE e.parenttype = 'Student' AND e.parent = s.student_id AND e.batch = %(batch)s
			ORDER BY e.idx LIMIT 1
		)
		LEFT JOIN `tabSchool` school ON school.name = enrollment.school
		WHERE s.rubric_eval_job = %(job_name)s
		ORDER BY s.name
		""",
		{"batch": batch, "job_name": job_name},
		as_dict=True,
	)
	rubrics = {}
	for row in frappe.get_all(
		"Rubric Evaluation",
		filters={"parenttype": "Submission", "rubric_eval_job": job_name},
		fields=[
			"parent", "skill", "grade_value", "observation", "final_grade", "submission_validity",
			"timestamp", "model_used",
		],
		order_by="parent, idx",
	):
		rubrics.setdefault(row.parent, []).append(row)

	max_skills = max((len(rows) for rows in rubrics.values()), default=0)
	header = [
		"Submission ID", "Student ID", "Submission Type", "Submission URL", "Assignment ID", "Created At",
		"Submission Validity", "Final Grade", "Plagiarism Status", "Overall Feedback", "Overall Feedback Translated",
		"Date", "Model",
	]
	for i in range(1, max_skills + 1):
		header += [f"Skill {i}", f"Grade Value {i}", f"Observation {i}"]
	header += ["Vertical", "Level", "State"]

	workbook = Workbook()
	workbook.calculation = CalcProperties(calcMode="auto", fullCalcOnLoad=True, forceFullCalc=True)
	summary = workbook.active
	summary.title = "Summary"
	worksheet = workbook.create_sheet("Evaluation Results")
	worksheet.freeze_panes = "A2"
	worksheet.append(header)
	for cell in worksheet[1]:
		cell.font = Font(bold=True)

	for s in submissions:
		rows = rubrics.get(s.name, [])
		first = rows[0] if rows else frappe._dict()
		line = [
			s.name, s.student_id, s.submission_type,
			(s.submission_url or "").replace("https://storage.googleapis.com/", "https://storage.cloud.google.com/", 1),
			s.assign_id, s.created_at, first.submission_validity, first.final_grade, s.plagiarism_status,
			s.overall_feedback, s.overall_feedback_translated,
			first.timestamp, first.model_used,
		]
		for r in rows:
			line += [r.skill, r.grade_value, r.observation]
		line += [None] * (3 * (max_skills - len(rows)))
		line += [s.vertical, s.level, s.state]
		worksheet.append([
			ILLEGAL_XLSX_CHARACTERS.sub("", value) if isinstance(value, str) else value
			for value in line
		])

	worksheet.auto_filter.ref = worksheet.dimensions
	bq = workbook.create_sheet("BQ stats")
	bq.append(["Student_ID", "Grade", "Level", "Vertical", "School_ID", "State"])
	bq.freeze_panes = "A2"
	for cell in bq[1]:
		cell.font = Font(bold=True)
		bq.column_dimensions[cell.column_letter].width = 18

	# Keep ranges tied to the generated headers, including every exported skill slot.
	columns = {cell.value: cell.column_letter for cell in worksheet[1]}
	bq_columns = {cell.value: cell.column_letter for cell in bq[1]}
	last_row = max(2, worksheet.max_row)
	result_ranges = {name: f"'Evaluation Results'!${col}$2:${col}${last_row}" for name, col in columns.items()}
	student_column = f"'BQ stats'!${bq_columns['Student_ID']}:${bq_columns['Student_ID']}"
	# SUMPRODUCT also evaluates the array expression after importing into Google Sheets.
	summary["I2"] = f'=MAX(2,SUMPRODUCT(MAX(({student_column}<>"")*ROW({student_column}))))'
	bq_ranges = {
		name: f"'BQ stats'!${col}$2:INDEX('BQ stats'!${col}:${col},$I$2)"
		for name, col in bq_columns.items()
	}
	filters = []
	bq_filters = []
	for row, (dimension, color) in enumerate((("Vertical", "E06666"), ("Level", "B6D7A8"), ("State", "FFE599")), 2):
		summary[f"E{row}"] = dimension
		summary[f"F{row}"] = "All"
		for col in "EF":
			summary[f"{col}{row}"].fill = PatternFill("solid", fgColor=color)
		selector = f"$F${row}"
		filters.append(f'((({selector}="All")+({result_ranges[dimension]}={selector}))>0)')
		bq_filters.append(f'((({selector}="All")+({bq_ranges[dimension]}={selector}))>0)')
		option_column = get_column_letter(row + 8)
		# Replacing blank options with All lets UNIQUE remove them without FILTER's
		# Excel-only serialization prefix, which Google Sheets does not import.
		options = ",".join((
			f'INDEX(IF({result_ranges[dimension]}="","All",{result_ranges[dimension]}),0)',
			f'INDEX(IF(({bq_ranges["Student_ID"]}="")+({bq_ranges[dimension]}=""),'
			f'"All",{bq_ranges[dimension]}),0)',
		))
		summary[f"{option_column}2"] = f'=_xlfn.UNIQUE(_xlfn.VSTACK("All",{options}))'
		validation = DataValidation(type="list", formula1=f"${option_column}:${option_column}")
		summary.add_data_validation(validation)
		validation.add(summary[f"F{row}"])
	summary.column_dimensions.group("I", "L", hidden=True)
	result_mask = "*".join([f'({result_ranges["Submission ID"]}<>"")', *filters])
	bq_mask = "*".join([f'({bq_ranges["Student_ID"]}<>"")', *bq_filters])

	summary["A2"] = "Total Students Registered (A)"
	summary["B2"] = frappe.db.sql(
		"""SELECT COUNT(DISTINCT parent) FROM `tabStudent Enrollment`
		WHERE parenttype = 'Student' AND batch = %s""", (batch,),
	)[0][0]
	for row, label, formula in (
		(3, "Out of (A) who watched the video", f"=SUMPRODUCT({bq_mask})"),
		(5, "Students who watched the video (B)", "=B3"),
		(6, "Out of (B) who submitted (C)", f"=SUMPRODUCT({result_mask})"),
		(7, "Out of (B) Valid Submission", f'=SUMPRODUCT({result_mask}*({result_ranges["Submission Validity"]}="valid"))'),
		(8, "Out of (C) Valid Submission", "=B7"),
	):
		summary[f"A{row}"] = label
		summary[f"B{row}"] = formula
	for row, denominator in ((3, 2), (6, 5), (7, 5), (8, 6)):
		summary[f"C{row}"] = f'=IF($B${denominator}=0,"",B{row}/$B${denominator})'
		summary[f"C{row}"].number_format = "0.00%"

	skill_pairs = [
		(name, f"Grade Value {name.removeprefix('Skill ')}")
		for name in columns if re.fullmatch(r"Skill \d+", name)
	]
	skills = sorted({
		cell.value for skill, _ in skill_pairs for cell in worksheet[columns[skill]][1:]
		if cell.value and cell.value.strip()
	})
	skill_colors = cycle(("D0E0E3", "9FC5E8", "6FA8DC", "A2C4C9", "B6D7A8"))
	for row, skill, color in zip(range(11, 11 + 5 * len(skills), 5), skills, skill_colors):
		for col, label in enumerate((skill, "Invalid", "Novice", "Beginner", "Emerging", "Proficient", "Expert"), 1):
			cell = summary.cell(row, col, label)
			cell.font = Font(bold=True)
			cell.fill = PatternFill("solid", fgColor=color)
		summary.cell(row + 1, 1, "Absolutes")
		summary.cell(row + 2, 1, "Percentage")
		for col, grade in enumerate((-1, 1, 2, 3, 4, 5), 2):
			matches = "+".join(
				f'(({result_ranges[skill_header]}=$A{row})*({result_ranges[grade_header]}={grade}))'
				for skill_header, grade_header in skill_pairs
			)
			summary.cell(row + 1, col, f"=SUMPRODUCT({result_mask}*(({matches})>0))").number_format = "0"
			denominator = 6 if grade == -1 else 7
			letter = get_column_letter(col)
			summary.cell(row + 2, col, f'=IF($B${denominator}=0,"",{letter}{row + 1}/$B${denominator})').number_format = "0.00%"
	summary.column_dimensions["A"].width = 48
	for col in "BCDEFG":
		summary.column_dimensions[col].width = 18
	for row in (2, 3, 5, 6, 7, 8):
		summary[f"B{row}"].number_format = "0"
	buffer = BytesIO()
	workbook.save(buffer)
	return buffer.getvalue()
