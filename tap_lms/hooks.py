from . import __version__ as app_version
from frappe import get_all


app_name = "tap_lms"
app_title = "Tap Lms"
app_publisher = "Techt4dev"
app_description = "Lms system for tap"
app_email = "tech4dev@gmail.com"
app_license = "MIT"


# Document Events
doc_events = {
    "School": {
        "before_save": "tap_lms.tap_lms.doctype.school.school.before_save"
    },
    # "Teacher": {
    #     "on_update": "tap_lms.glific_webhook.update_glific_contact"
    # },
    # "StudentStageProgress": {
    #     "after_insert": "tap_lms.tap_lms.doctype.studentonboardingprogress.studentonboardingprogress.update_student_progress",
    #     "on_update": "tap_lms.tap_lms.doctype.studentonboardingprogress.studentonboardingprogress.update_student_progress"
    # },
    # CR-002 v2 gamification (2026-05-13): VideoClass completion via
    # StudentContentLog drives the activity-points handler. The handler also
    # arms the grace clock on the first VideoClass of each week (CR-003
    # follow-up 2: atomic Postgres CASE WHEN on `weekly_video_done`). Without
    # this hook the activity-points pipeline AND the grace clock are dead.
    # "StudentContentLog": {
    #     "after_insert": "tap_lms.summer_program.activity_points.handle_content_log"
    # },
    # CR-002 v2 gamification (2026-05-13): quiz attempts award per-question
    # points (correct → q.points; wrong → q.failed_points). The handler is
    # idempotent via `attempt.points_earned` so re-saves of completed
    # attempts are no-ops.
    # "StudentQuizAttempt": {
    #     "on_update": "tap_lms.summer_program.quiz_points.handle_attempt_update"
    # }
}

# Scheduled Tasks
#
# Daily:
#   - update_incomplete_stages: legacy onboarding sweep (pre-existing)
#   - run_daily_actions: SP daily housekeeping (scheduler.py)
#   - check_auto_activate: SP — auto-activates BPRs whose batch.start_date has
#                          arrived; seeds next_action_at on PEs so the per-PE
#                          dispatcher has work. See task #19 for details.
#
# Cron:
#   - */1 * * * *  — pe_dispatcher: per-PE event-driven dispatcher (task #15);
#                    processes overdue next_action_at, routes by next_action_type.
#                    Tightened from */2 to */1 min for the 100K-student MVP target
#                    (architecture §8.8 + ADR-003 audit log 2026-05-13). Combined
#                    with DISPATCH_BATCH_SIZE=1000 and 4 parallel workers gives
#                    240K actions/hour — drains a 100K week-boundary T19 burst in
#                    ~25 min. Hard prerequisite: partial index idx_pe_next_action
#                    (task #24, patch cr_004_scale.idx_pe_next_action) so the
#                    SELECT stays <50ms at scale.
#   - 0 */2 * * *  — escalation_runner: 6-hour bulk escalation sweep (legacy
#                    batcher; will eventually be replaced by escalation_batcher
#                    in collection-mode rollout)
#   - 0 0 * * 1    — auto_advance_batch_week: weekly Monday sweep that bumps
#                    Batch.current_calendar_week and unblocks max_allowed_week
#                    on each PE
#   - 30 20 * * *  — student sheet registration daily run; 20:30 UTC = 02:00 IST
scheduler_events = {
    "cron": {
        # Teacher submission images: pull gcs_urls from BigQuery and attach them
        # to the matching submission. Runs every night at 2 am. 
        # The job exits immediately unless BigQuery Settings is
        # enabled, so this is inert until someone turns it on.
        "0 2 * * *": [
            "tap_lms.teacher_bot.bigquery_sync.sync_submission_images",
        ],
        # Student feedback delivery (Glific feedback flow), three times a day.
        # Cron expressions use the Frappe system timezone (Asia/Kolkata):
        #   0 15 * * *  — 15:00 IST
        #   30 16 * * * — 16:30 IST
        #   0 21 * * *  — 21:00 IST
        # Feedback is generated continuously; only the Glific flow start is
        # batched. No-op if "Enable Batch Feedback Delivery" is
        # unchecked in Feedback Delivery Settings.
        "0 15 * * *": [
            "tap_lms.imgana.feedback_delivery.dispatch_1500_ist",
        ],
        "30 16 * * *": [
            "tap_lms.imgana.feedback_delivery.dispatch_1630_ist",
        ],
        "0 21 * * *": [
            "tap_lms.imgana.feedback_delivery.dispatch_2100_ist",
        ],
        "30 20 * * *": [
            "tap_lms.tap_lms.doctype.student_sheet_registration_job."
            "student_sheet_registration_job.enqueue_daily_student_sheet_registration",
        ],
    },
    # "daily": [
    #     "tap_lms.tap_lms.page.onboarding_flow_trigger.onboarding_flow_trigger.update_incomplete_stages",
    #     "tap_lms.summer_program.scheduler.run_daily_actions",
    #     "tap_lms.summer_program.batch_activation.check_auto_activate",
    # ],
    # "cron": {
    #     "*/1 * * * *": [
    #         "tap_lms.summer_program.pe_dispatcher.process_program_actions",
    #     ],
    #     # Retired 2026-05-21 (task #50): the legacy escalation_runner ran in
    #     # parallel with pe_dispatcher.handle_escalation (system A, post-CR-003)
    #     # without gating on canonical PE state. It double-escalated students
    #     # who already submitted and disagreed with the per-PE escalation
    #     # counter. The per-PE dispatcher fires escalations correctly when
    #     # next_action_at + next_action_type='escalation' are armed by T1
    #     # (content_no_response). escalation_runner.py is preserved as dead
    #     # code for one release cycle; remove the module after launch.
    #     # "0 */2 * * *": [
    #     #     "tap_lms.summer_program.escalation_runner.run_escalation_check",
    #     # ],
    #     "0 0 * * 1": [
    #         "tap_lms.summer_program.batch_admin.auto_advance_batch_week",
    #     ],
    #     # CR-027 (2026-06-09): Monday 06:00 UTC weekly content sweep. Runs
    #     # AFTER auto_advance_batch_week (Monday 00:00 UTC) bumps
    #     # Batch.current_calendar_week, so the sweep sees the new calendar week.
    #     # Phase 1 demotes behind students (wk < calendar_week, no video) to
    #     # normal_escalation via t2_start_escalation; Phase 2 delivers this
    #     # week's content to current-week candidates via a temp Glific sweep
    #     # collection. Requires `bench migrate` to register this entry in
    #     # tabScheduledJobType (L-049) — `bench restart` alone is not enough.
    #     "0 6 * * 1": [
    #         "tap_lms.summer_program.scheduler.weekly_content_sweep",
    #     ],
    #     # CR-005 (2026-05-15): Tuesday 03:30 UTC = Tuesday 09:00 IST.
    #     # Fires SP_Content_Delivery against each active BPR's `main`
    #     # Glific collection. Membership is maintained continuously by
    #     # state-machine transitions (Approach B) — this cron just fires.
    #     "30 3 * * 2": [
    #         "tap_lms.summer_program.scheduler.weekly_content_delivery_trigger",
    #     ],
    #     # Task #56 (2026-05-16): hourly watchdog for PEs stuck in
    #     # feedback_ready because Glific's F5 callback dropped silently.
    #     # LOG-only — does NOT auto-transition; operator replays manually.
    #     # See pre_launch.feedback_ready_watchdog for full rationale.
    #     # Task #17 (2026-05-28): two more hourly watchers added at the same
    #     # cadence. Both are read-only — they turn silent async failures
    #     # (Glific sync DLQ, RQ queue depth) into Error Log entries operators
    #     # can see in the Frappe Desk Error Log list view.
    #     "0 * * * *": [
    #         "tap_lms.summer_program.pre_launch.feedback_ready_watchdog",
    #         "tap_lms.summer_program.scheduler.glific_sync_dlq_watcher",
    #         "tap_lms.summer_program.scheduler.rq_queue_depth_watcher",
    #         # CR-025 (2026-06-09): hourly probe to detect stale Glific tokens.
    #         # Invalidates the stored token when it gets a 401, so the next real
    #         # API call triggers a fresh login instead of queuing 6 retries against
    #         # a dead token. Requires `bench migrate` to register this scheduler
    #         # entry in tabScheduledJobType (L-049).
    #         "tap_lms.glific_integration.probe_token_health",
    #     ],
    #     # Task #97 / removed 2026-05-26 (L-027 MVP discipline):
    #     # `periodic_glific_reconcile` was wired here at */10 cadence as a
    #     # drift safety net. After diagnosing the real root cause of the
    #     # Himani / ST00051295 rendering bug (missing createContactsField
    #     # definitions, NOT value drift), the cron stopped being MVP-
    #     # justified — in production normal operation, students don't have
    #     # Desk access and code paths fire the sync hook correctly. The
    #     # `set_value` bypass is operator-driven (console backfills).
    #     #
    #     # The function `scheduler.periodic_glific_reconcile` is preserved
    #     # for MANUAL invocation from bench console when needed. Same for
    #     # `dev_tools.reconcile_pe_to_glific(pe_name)` and
    #     # `dev_tools.reconcile_batch_to_glific(batch_name)`.
    #     #
    #     # If post-launch logs show silent value drift, re-enable here at a
    #     # cadence appropriate to the observed frequency (likely daily, not
    #     # */10) — uncomment and `bench migrate`.
    #     # "*/10 * * * *": [
    #     #     "tap_lms.summer_program.scheduler.periodic_glific_reconcile",
    #     # ],
    # },
}

# Page configurations
# page_js = {"onboarding-flow-trigger": "public/js/onboarding_flow_trigger.js"}

# Reports
report_script_custom_doctypes = ["StudentStageProgress"]


# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/tap_lms/css/tap_lms.css"
# app_include_js = "/assets/tap_lms/js/tap_lms.js"

# include js, css files in header of web template
# web_include_css = "/assets/tap_lms/css/tap_lms.css"
# web_include_js = "/assets/tap_lms/js/tap_lms.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "tap_lms/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
#       "Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
#       "methods": "tap_lms.utils.jinja_methods",
#       "filters": "tap_lms.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "tap_lms.install.before_install"
# after_install = "tap_lms.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "tap_lms.uninstall.before_uninstall"
# after_uninstall = "tap_lms.uninstall.after_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "tap_lms.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
#       "Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
#       "Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
#       "ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
#       "*": {
#               "on_update": "method",
#               "on_cancel": "method",
#               "on_trash": "method"
#       }
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
#       "all": [
#               "tap_lms.tasks.all"
#       ],
#       "daily": [
#               "tap_lms.tasks.daily"
#       ],
#       "hourly": [
#               "tap_lms.tasks.hourly"
#       ],
#       "weekly": [
#               "tap_lms.tasks.weekly"
#       ],
#       "monthly": [
#               "tap_lms.tasks.monthly"
#       ],
# }

# Testing
# -------

# before_tests = "tap_lms.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
#       "frappe.desk.doctype.event.event.get_events": "tap_lms.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
#       "Task": "tap_lms.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]


# User Data Protection
# --------------------

# user_data_fields = [
#       {
#               "doctype": "{doctype_1}",
#               "filter_by": "{filter_by}",
#               "redact_fields": ["{field_1}", "{field_2}"],
#               "partial": 1,
#       },
#       {
#               "doctype": "{doctype_2}",
#               "filter_by": "{filter_by}",
#               "partial": 1,
#       },
#       {
#               "doctype": "{doctype_3}",
#               "strict": False,
#       },
#       {
#               "doctype": "{doctype_4}"
#       }
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
#       "tap_lms.auth.validate"
# ]

fixtures = [{ "doctype": "Client Script", "filters": [ ["module", "in", ( "Tap Lms" )] ] }]
