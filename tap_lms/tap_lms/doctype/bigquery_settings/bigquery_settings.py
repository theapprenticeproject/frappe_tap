# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class BigQuerySettings(Document):
	pass


@frappe.whitelist()
def clear_parquet_cache():
	"""Called by the "Clear Parquet Cache" button on this doctype.

	Restricted to System Manager and delegates all filesystem work to
	teacher_reporting.clear_parquet_cache, which does the safe
	rename-then-remove dance so a crash mid-clear can't leave a
	half-deleted local state file behind, and refuses to run while a
	weekly extract genuinely holds the run lock.

	This only clears local state (pdf_manifest.json, missing_schools.json,
	the local event log, and any legacy parquet/staging/live directories
	from before PDFs were generated at extract time). It never touches
	the PDFs already uploaded to GCS.
	"""
	frappe.only_for("System Manager")

	from tap_lms.bigquery.teacher.teacher_reporting import (
		clear_parquet_cache as clear_local_state,
	)

	try:
		result = clear_local_state()
	except ValueError as exc:
		frappe.throw(str(exc))
	except Exception:
		frappe.log_error(frappe.get_traceback(), "BigQuery Settings: clear_local_state failed")
		frappe.throw("Failed to clear local state. Check the Error Log for details.")

	if result.get("status") == "noop":
		frappe.msgprint(result.get("message") or "Nothing to clear.")
	else:
		frappe.msgprint(
			f"Cleared {result.get('count', 0)} local path(s). GCS PDFs were not touched."
		)

	return result


@frappe.whitelist()
def teacher_reporting_stats():
	"""Called by the "View Stats" button on this doctype. Pure local
	read -- no BigQuery or GCS calls -- safe to click repeatedly.
	"""
	frappe.only_for("System Manager")

	from tap_lms.bigquery.teacher.teacher_reporting import get_teacher_reporting_stats

	return get_teacher_reporting_stats()