frappe.ui.form.on("Learning Unit Import Source", {
	setup(frm) {
		frm.set_query("gcs_settings", () => ({
			query:
				"tap_lms.tap_lms.doctype.learning_unit_import_source.learning_unit_import_source.gcs_settings_query",
		}));
	},
});
