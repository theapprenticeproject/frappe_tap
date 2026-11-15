import json

import frappe


JOBS = {
	"teacher_submission_sync.sync_submission_images": {
		"messages_table": "messages",
		"media_table": "messages_media",
		"lookback_days": 2,
		"match_window_minutes": 5,
		"batch_limit": 200,
		"source_timezone": "UTC",
		"flow_label_filter": "%image_capture%",
	},
	"teacher_reporting.run_weekly_extract": {
		"dataset_id": "918454812392",
		"contacts_table": "contacts",
		"messages_table": "messages",
		"group_label_pattern": r"(^|,)\s*(TLM26_AllStudents_Batch1|TLM26_AllStudents_Batch2|TLM26_AllStudents_Batch3|TLM26_AllStudents_Batch MCD|TLM26_AllStudents_Batch Moga_P|TLM26_AllStudents_Batch UP)\s*(,|$)",
		"main_flow_name": "%Tlm26_MainFlow_FirstStep%",
		"watched_flow_label": "%Activity_Access%",
		"shared_flow_label": "%Activity_Submission%",
		"gcs_settings_name": "dummy_central-phalanx-297915-Authenticated",
		"max_query_gb": 10,
		"max_workers": 8,
		"batch_size": 200,
		"signed_url_ttl_minutes": 10080,
		"stats_ttl_days": 7,
	},
	"feedback_delivery.dispatch_1500_ist": {},
	"feedback_delivery.dispatch_1630_ist": {},
	"feedback_delivery.dispatch_2100_ist": {},
	"student_sheet_registration_job.enqueue_daily_student_sheet_registration": {},
	"on_demand_evaluation.check_queued_evaluations": {},
}

DISABLED_JOBS = {
	"teacher_submission_sync.sync_submission_images",
	"teacher_reporting.run_weekly_extract",
}


def execute():
	frappe.reload_doc("tap_lms", "doctype", "custom_scheduled_jobs")

	for name, parameters in JOBS.items():
		if frappe.db.exists("Custom Scheduled Jobs", name):
			job = frappe.get_doc("Custom Scheduled Jobs", name)
		else:
			job = frappe.get_doc({"doctype": "Custom Scheduled Jobs", "job_key": name})
		job.enabled = int(name not in DISABLED_JOBS)
		job.job_parameters = json.dumps(parameters, ensure_ascii=True)
		job.save(ignore_permissions=True)
