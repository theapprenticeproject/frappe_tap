const METHOD = "tap_lms.tap_lms.doctype.on_demand_evaluation.on_demand_evaluation";

frappe.ui.form.on("On Demand Evaluation", {
    refresh(frm) {
        if (frm.is_new() || frm.doc.status !== "Draft") {
            return;
        }
        frm.add_custom_button(__("Count Assignments & Submissions"), async () => {
            if (frm.is_dirty()) {
                await frm.save();
            }
            frappe.call({
                method: `${METHOD}.count_evaluations`,
                args: { docname: frm.doc.name },
                freeze: true,
                freeze_message: __("Counting..."),
                callback({ message }) {
                    frm.reload_doc();
                    frappe.msgprint(
                        __("Assignments found: {0}<br>Submissions that will be queued: {1}", [
                            message.total_assignments,
                            message.total_submissions,
                        ])
                    );
                },
            });
        });
        frm.add_custom_button(__("Queue Evaluations"), () => {
            frappe.call({
                method: `${METHOD}.queue_evaluations`,
                args: { docname: frm.doc.name },
                freeze: true,
                callback() {
                    frappe.show_alert({ message: __("Queueing job started"), indicator: "blue" });
                },
            });
        });
    },
});
