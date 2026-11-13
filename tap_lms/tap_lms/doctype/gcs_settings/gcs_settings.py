# Copyright (c) 2025, Techt4dev and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint


class GCSSettings(Document):
	def validate(self):
		if not cint(self.enabled):
			return

		if not self.bucket_name:
			frappe.throw("Bucket Name is required when GCS Storage is enabled.")