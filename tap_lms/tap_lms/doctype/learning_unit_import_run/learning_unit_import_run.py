# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime

from tap_lms.learning_unit_import.engine import parse_unit_filters
from tap_lms.learning_unit_import.runner import JOB_TIMEOUT, RUN_SOURCE_DOCTYPE, enqueue_run


ACTIVE_STATUSES = ("Queued", "Running")
SOURCE_RESULT_FIELDS = ("created_count", "updated_count", "skipped_count", "error_count", "error_message", "log")


class LearningUnitImportRun(Document):
	def validate(self):
		previous = self.get_doc_before_save()
		if previous and previous.status != "Draft":
			frappe.throw("This run has already started and can't be edited. Create a new run instead.")

		for row in self.sources:
			try:
				parse_unit_filters(row.unit_filters or "")
			except ValueError as exc:
				frappe.throw(f"Sources row {row.idx} ({row.source}): {exc}")

			# Rows copied from an earlier run start fresh.
			row.status = "Pending"
			for fieldname in SOURCE_RESULT_FIELDS:
				row.set(fieldname, None)

	@frappe.whitelist()
	def start_import(self):
		self.check_permission("write")
		if self.status != "Draft":
			frappe.throw(f"This run is already {self.status}.")

		active_run = self.find_active_run_with_same_source()
		if active_run:
			frappe.throw(f"Run {active_run} is still in progress for one of these sources.")

		self.db_set("status", "Queued")
		enqueue_run(self.name)

	def find_active_run_with_same_source(self):
		sources = list({row.source for row in self.sources})
		other_runs = frappe.get_all(
			RUN_SOURCE_DOCTYPE,
			filters={
				"parenttype": "Learning Unit Import Run",
				"source": ["in", sources],
				"parent": ["!=", self.name],
			},
			pluck="parent",
			distinct=True,
		)
		if not other_runs:
			return None

		# Ignore runs whose worker died without updating their status.
		return frappe.db.get_value(
			"Learning Unit Import Run",
			{
				"name": ["in", other_runs],
				"status": ["in", ACTIVE_STATUSES],
				"modified": [">", add_to_date(now_datetime(), seconds=-JOB_TIMEOUT)],
			},
			"name",
		)
