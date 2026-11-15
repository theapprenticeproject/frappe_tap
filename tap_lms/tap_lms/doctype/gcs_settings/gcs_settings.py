# Copyright (c) 2025, Techt4dev and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document
from frappe.utils import cint


class GCSSettings(Document):
	def before_naming(self):
		self.service_account_name = self._get_service_account_name()

	def validate(self):
		self.service_account_name = self._get_service_account_name()

		if not cint(self.enabled):
			return

		if not self.bucket_name:
			frappe.throw("Bucket Name is required when GCS Storage is enabled.")

	def _get_service_account_name(self):
		try:
			credentials = json.loads(self.credentials_json or "{}")
		except (TypeError, json.JSONDecodeError):
			return ""

		email = str(credentials.get("client_email") or "").strip()
		suffix = ".iam.gserviceaccount.com"
		return email[:-len(suffix)] if email.endswith(suffix) else email
