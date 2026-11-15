from __future__ import annotations

import json

import frappe


def get_job_config(name: str) -> frappe._dict:
	job = frappe.get_cached_doc("Custom Scheduled Jobs", name)
	try:
		parameters = json.loads(job.job_parameters or "{}")
	except json.JSONDecodeError as exc:
		raise frappe.ValidationError(
			f"Custom Scheduled Jobs {name} has invalid JSON parameters"
		) from exc

	if not isinstance(parameters, dict):
		raise frappe.ValidationError(
			f"Custom Scheduled Jobs {name} parameters must be a JSON object"
		)

	return frappe._dict(enabled=bool(job.enabled), parameters=frappe._dict(parameters))
