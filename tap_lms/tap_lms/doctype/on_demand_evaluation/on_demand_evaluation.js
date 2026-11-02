frappe.ui.form.on("On Demand Evaluation", {
    refresh(frm) {
        if (frm.is_new() || frm.doc.status !== "Draft") {
            return;
        }
        frm.add_custom_button(__("Queue Evaluations"), () => {
            frappe.call({
                method: "tap_lms.tap_lms.doctype.on_demand_evaluation.on_demand_evaluation.queue_evaluations",
                args: { docname: frm.doc.name },
                freeze: true,
                callback() {
                    frappe.show_alert({ message: __("Queueing job started"), indicator: "blue" });
                },
            });
        });
    },
});
