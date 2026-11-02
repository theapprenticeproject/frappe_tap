// Copyright (c) 2026, Techt4dev and contributors
// For license information, please see license.txt

const FINISHED_STATUSES = ["Completed", "Completed With Errors", "Failed"];

frappe.ui.form.on("Learning Unit Import Run", {
	setup(frm) {
		frappe.realtime.on("learning_unit_import_progress", (data) => {
			if (data.run !== frm.doc.name) return;
			frm.dashboard.set_headline_alert(
				`${frappe.utils.escape_html(data.source)} &middot; ${__("{0} activities processed", [
					data.processed,
				])} &middot; ${frappe.utils.escape_html(data.message)}`,
				"blue"
			);
		});
		frappe.realtime.on("learning_unit_import_done", (data) => {
			if (data.run !== frm.doc.name) return;
			frm.dashboard.clear_headline();
			frm.reload_doc();
		});
	},

	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.status !== "Draft") {
			frm.disable_form();
		}

		if (frm.doc.status === "Draft") {
			const label = frm.doc.dry_run ? __("Start Dry Run") : __("Start Import");
			frm.add_custom_button(label, () => start_import(frm)).addClass("btn-primary");
		}

		if (["Queued", "Running"].includes(frm.doc.status)) {
			frm.dashboard.set_headline_alert(__("Import is {0}...", [frm.doc.status.toLowerCase()]), "blue");
		}

		if (FINISHED_STATUSES.includes(frm.doc.status)) {
			if (frm.doc.dry_run) {
				frm.add_custom_button(__("Run For Real"), () => new_run_from(frm, { dry_run: 0 })).addClass(
					"btn-primary"
				);
			}
			frm.add_custom_button(__("New Run From This"), () => new_run_from(frm, {}));
		}
	},
});

function start_import(frm) {
	const run = () =>
		frm.call("start_import").then(() => {
			frappe.show_alert({ message: __("Import queued"), indicator: "blue" });
			frm.reload_doc();
		});

	const confirm_and_run = () => {
		if (frm.doc.dry_run) {
			run();
			return;
		}
		frappe.confirm(
			frm.doc.update_existing
				? __("This will create and update LMS records and write assignment IDs to the assignment flow sheet. Continue?")
				: __("This will create LMS records and write assignment IDs to the assignment flow sheet. Continue?"),
			run
		);
	};

	if (frm.is_dirty()) {
		frm.save().then(confirm_and_run);
	} else {
		confirm_and_run();
	}
}

function new_run_from(frm, overrides) {
	const new_doc = frappe.model.copy_doc(frm.doc);
	Object.assign(new_doc, overrides);
	frappe.set_route("Form", new_doc.doctype, new_doc.name);
}
