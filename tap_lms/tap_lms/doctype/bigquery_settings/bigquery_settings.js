// Copyright (c) 2026, Techt4dev and contributors
// For license information, please see license.txt

frappe.ui.form.on("BigQuery Settings", {
	refresh(frm) {
		frm.add_custom_button(
			__("Clear Parquet Cache"),
			function () {
				frappe.confirm(
					__(
						"This deletes the local teacher-reporting report index " +
						"(pdf_manifest.json) and any leftover files from older versions " +
						"of this feature. It does NOT delete the PDFs already uploaded to " +
						"GCS, and does NOT affect the on-demand endpoint for schools whose " +
						"PDFs are still in GCS from the last run, until the index is rebuilt " +
						"by the next weekly extract. This cannot be undone. Continue?"
					),
					function () {
						frappe.call({
							method:
								"tap_lms.tap_lms.doctype.bigquery_settings.bigquery_settings.clear_parquet_cache",
							freeze: true,
							freeze_message: __("Clearing local cache..."),
							callback: function (r) {
								if (!r.exc) {
									frm.reload_doc();
								}
							},
						});
					}
				);
			},
			__("Teacher Reporting")
		);
	},
});