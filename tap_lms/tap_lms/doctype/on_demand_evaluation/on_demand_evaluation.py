# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

import csv
import io

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime
from google.auth.transport.requests import AuthorizedSession

from tap_lms.imgana.submission import enqueue_submission
from tap_lms.onboarding.backend_upload_utils import (
	FAILED_ROWS_GCP_PROJECT_ID,
	get_google_service_account_credentials,
	upload_bytes_to_gcs,
)

# Built by scripts/private/list_prod_learning_unit_names_to_google_sheet.py:
# one tab per "<vertical> <level>".
SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets/1nSN72ikEqM7U0gMl-3AoOV8KeHAzNZvK_zHCNcT6B34"


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
		frappe.throw(f"No submissions found for batch {doc.batch} and assignments {sorted(assignment_ids)}.")

	# Commit the job link before publishing so results can never arrive ahead of it.
	frappe.db.sql(
		"""UPDATE `tabSubmission` SET rubric_eval_status = 'Processing', rubric_eval_job = %s
		WHERE name IN %s""",
		(docname, tuple(submissions)),
	)
	doc.db_set({"status": "Queued", "total_submissions": len(submissions), "queued_at": now_datetime()})
	frappe.db.commit()

	for name in submissions:
		enqueue_submission(name, eval_type="rubric")


@frappe.whitelist()
def count_evaluations(docname):
	doc = frappe.get_doc("On Demand Evaluation", docname)
	assignment_ids = _get_assignment_ids(doc)
	return {"assignments": len(assignment_ids), "submissions": len(_get_submissions(doc, assignment_ids))}


def _get_submissions(doc, assignment_ids):
	if not assignment_ids:
		return []
	submissions = frappe.db.sql_list(
		"""
		SELECT s.name FROM `tabSubmission` s
		WHERE s.assign_id IN %(assignment_ids)s
		  AND EXISTS (
			SELECT 1 FROM `tabStudent Enrollment` e
			WHERE e.parenttype = 'Student' AND e.parent = s.student_id AND e.batch = %(batch)s
		  )
		""",
		{"assignment_ids": tuple(assignment_ids), "batch": doc.batch},
	)
	return submissions[: doc.test_limit] if doc.test_limit else submissions


def _get_assignment_ids(doc):
	verticals = [row.vertical for row in doc.verticals] or (
		frappe.get_meta("On Demand Evaluation Vertical").get_field("vertical").options.split("\n")
	)
	session = AuthorizedSession(get_google_service_account_credentials())
	metadata = _get_json(session, SHEETS_URL, {"fields": "sheets.properties.title"})
	titles = [
		sheet["properties"]["title"]
		for sheet in metadata["sheets"]
		if any(sheet["properties"]["title"].startswith(f"{vertical} ") for vertical in verticals)
	]
	if not titles:
		frappe.throw(f"No sheet tabs found for verticals {verticals}.")

	ranges = ["'{}'".format(title.replace("'", "''")) for title in titles]
	data = _get_json(session, f"{SHEETS_URL}/values:batchGet", {"ranges": ranges})

	unit_activity_no = _normalize(doc.unit_activity_no)
	program = _normalize(doc.program)
	assignment_ids = set()
	for value_range in data["valueRanges"]:
		header, *rows = value_range.get("values") or [[]]
		unit_col = header.index("Unit & Activity No.")
		program_col = header.index("Program")
		assignment_col = header.index("Assignment")
		for row in rows:
			row = row + [""] * (len(header) - len(row))
			if _normalize(row[unit_col]) == unit_activity_no and _normalize(row[program_col]) == program:
				assignment_ids.update(a.strip() for a in row[assignment_col].split(",") if a.strip())
	return assignment_ids


def _get_json(session, url, params):
	response = session.get(url, params=params, timeout=120)
	response.raise_for_status()
	return response.json()


def _normalize(value):
	return " ".join(str(value).split()).lower()


def check_queued_evaluations():
	"""Cron: complete Queued jobs once none of their submissions is still Processing."""
	for name in frappe.get_all("On Demand Evaluation", filters={"status": "Queued"}, pluck="name"):
		if not frappe.db.exists("Submission", {"rubric_eval_job": name, "rubric_eval_status": "Processing"}):
			url = upload_bytes_to_gcs(
				_build_csv(name), f"rubric/{name}.csv", FAILED_ROWS_GCP_PROJECT_ID, content_type="text/csv"
			)
			frappe.db.set_value("On Demand Evaluation", name, {
				"status": "Completed",
				"completed_at": now_datetime(),
				"output_file_url": url,
			})
			frappe.db.commit()


def _build_csv(job_name):
	submissions = frappe.get_all(
		"Submission",
		filters={"rubric_eval_job": job_name},
		fields=[
			"name", "student_id", "submission_type", "submission_url", "assign_id", "created_at",
			"plagiarism_status", "overall_feedback", "overall_feedback_translated",
		],
		order_by="name",
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

	buffer = io.StringIO()
	writer = csv.writer(buffer)
	writer.writerow(header)
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
		writer.writerow(line)
	return buffer.getvalue().encode("utf-8")
