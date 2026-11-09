frappe.ui.form.on("Student Bulk Import Job", {
    refresh(frm) {
        render_student_bulk_import_output(frm);

        if (frm.is_new()) {
            return;
        }

        if (frm.doc.status !== "Processing" && frm.doc.status !== "Queued") {
            frm.add_custom_button(__("Start Processing"), () => {
                frappe.call({
                    method: "tap_lms.tap_lms.doctype.student_bulk_import_job.student_bulk_import_job.start_student_bulk_import_job",
                    args: { docname: frm.doc.name },
                    freeze: true,
                    freeze_message: __("Queueing student bulk import..."),
                    callback(r) {
                        if (r.message) {
                            frappe.show_alert({
                                message: __("Student bulk import queued"),
                                indicator: "blue",
                            });
                            frm.reload_doc();
                        }
                    },
                });
            }, __("Actions"));
        }

        frm.add_custom_button(__("Refresh Status"), () => frm.reload_doc(), __("Actions"));
    },
});

function parse_json_value(value) {
    if (!value) {
        return null;
    }
    if (typeof value === "object") {
        return value;
    }
    try {
        return JSON.parse(value);
    } catch (error) {
        return null;
    }
}

function render_preformatted(frm, fieldname, text, empty_message) {
    const field = frm.get_field(fieldname);
    if (!field || !field.$wrapper) {
        return;
    }
    const content = text || empty_message;
    const pre = $("<pre>")
        .text(content)
        .css({
            "background-color": "var(--control-bg)",
            border: "1px solid var(--border-color)",
            "border-radius": "6px",
            "font-size": "12px",
            "line-height": "1.5",
            margin: "0",
            "max-height": "500px",
            overflow: "auto",
            padding: "12px",
            "white-space": "pre-wrap",
            "word-break": "break-word",
        });
    field.$wrapper.empty().append(pre);
}

function render_student_bulk_import_output(frm) {
    const summary = parse_json_value(frm.doc.summary_json);
    render_preformatted(
        frm,
        "summary_json_display",
        summary ? JSON.stringify(summary, null, 2) : frm.doc.summary_json,
        __("No summary is available yet."),
    );

    const log = parse_json_value(frm.doc.processing_log);
    const entries = log && Array.isArray(log.entries) ? log.entries : [];
    const readable_log = entries.map((entry) => {
        const timestamp = entry.timestamp || "-";
        return `[${timestamp}] ${entry.message || ""}`;
    }).join("\n\n");
    render_preformatted(
        frm,
        "processing_log_display",
        readable_log || (!log ? frm.doc.processing_log : ""),
        __("No processing log is available yet."),
    );
}
