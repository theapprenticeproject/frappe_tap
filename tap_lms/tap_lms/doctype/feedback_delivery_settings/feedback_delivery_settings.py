import frappe
from frappe import _
from frappe.model.document import Document


class FeedbackDeliverySettings(Document):
	def validate(self):
		for fieldname in (
			"calls_per_second",
			"batch_size",
			"job_timeout",
			"run_stale_hours",
			"max_attempts",
			"lease_minutes",
			"max_age_days",
		):
			if (self.get(fieldname) or 0) <= 0:
				frappe.throw(
					_("{0} must be greater than zero").format(self.meta.get_label(fieldname))
				)
