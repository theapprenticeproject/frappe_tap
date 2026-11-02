# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from tap_lms.learning_unit_import.sheets import parse_sheet_ref


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
