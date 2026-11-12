// Copyright (c) 2026, Techt4dev and contributors
// For license information, please see license.txt

frappe.ui.form.on("BigQuery Settings", {
	refresh(frm) {
		frm.add_custom_button(
			__("Clear Parquet Cache"),
			function () {
				frappe.confirm(
					__(
						"This deletes the on-disk teacher-reporting parquet cache " +
						"(live data, any in-progress staging write, and the rollback copy). " +
						"The on-demand PDF endpoint will have nothing to serve until the next " +
						"weekly extract runs. This cannot be undone. Continue?"
					),
					function () {
						frappe.call({
							method:
								"tap_lms.tap_lms.doctype.bigquery_settings.bigquery_settings.clear_parquet_cache",
							freeze: true,
							freeze_message: __("Clearing parquet cache..."),
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