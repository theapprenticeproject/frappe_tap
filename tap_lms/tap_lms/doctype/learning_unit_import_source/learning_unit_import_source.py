# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from tap_lms.learning_unit_import.sheets import parse_sheet_ref


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def gcs_settings_query(doctype, txt, searchfield, start, page_len, filters):
	search_term = f"%{txt}%"
	return frappe.get_list(
		"GCS Settings",
		or_filters={
			"name": ["like", search_term],
			"project_id": ["like", search_term],
		},
		fields=["name", "project_id"],
		limit_start=start,
		limit_page_length=page_len,
		order_by="modified desc",
		as_list=True,
	)


class LearningUnitImportSource(Document):
	def validate(self):
		for fieldname in ("spreadsheet_url", "assignment_flow_spreadsheet_url"):
			url = self.get(fieldname)
			if url:
				try:
					parse_sheet_ref(url)
				except ValueError:
					frappe.throw(f"{self.meta.get_label(fieldname)} is not a Google Sheets link: {url}")

		if self.source_type == "Source Tabs" and not self.source_tabs:
			frappe.throw("Add at least one row to Source Tabs.")
