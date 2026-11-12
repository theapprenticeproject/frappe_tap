# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class BigQuerySettings(Document):
	pass


@frappe.whitelist()
def clear_parquet_cache():
	"""Called by the "Clear Parquet Cache" button on this doctype.

	Restricted to System Manager (same role required to write this
	single doctype) and delegates all of the actual filesystem work to
	teacher_reporting.clear_parquet_cache, which does the safe
	rename-then-remove dance so a crash mid-clear can't leave a
	half-deleted cache directory behind. Any failure is logged and
	re-raised as a frappe.ValidationError so the user sees a clean
	message in the UI instead of a traceback.
	"""
	frappe.only_for("System Manager")

	from tap_lms.bigquery.teacher.teacher_reporting import (
		clear_parquet_cache as _clear_parquet_cache,
	)

	try:
		result = _clear_parquet_cache()
	except ValueError as exc:
		frappe.throw(str(exc))
	except Exception:
		frappe.log_error(frappe.get_traceback(), "BigQuery Settings: clear_parquet_cache failed")
		frappe.throw("Failed to clear the parquet cache. Check the Error Log for details.")

	if result.get("status") == "noop":
		frappe.msgprint(result.get("message") or "Nothing to clear.")
	else:
		frappe.msgprint(
			f"Cleared {result.get('count', 0)} cache path(s)."
		)

	return result