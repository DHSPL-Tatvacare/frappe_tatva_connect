from tatva_connect.whatsapp import roles as whatsapp_roles

app_name = "tatva_connect"
app_title = "Tatva Connect"
app_publisher = "TatvaCare"
app_description = "TatvaCare custom Frappe app: WATI WhatsApp, Acefone telephony, CRM overrides"
app_email = "pareekshith.bompally@tatvacare.in"
app_license = "mit"

# App logo (desk switcher header + /apps tile); ships as a committed asset, republished by `bench build` each deploy.
app_logo_url = "/assets/tatva_connect/images/tatva-connect.png"

# Tatva Connect desk-switcher app; six child workspaces via child Desktop Icons, gated by has_permission (System/Sales Manager).
add_to_apps_screen = [
	{
		"name": "tatva_connect",
		"title": "Tatva Connect",
		"logo": "/assets/tatva_connect/images/tatva-connect.png",
		"route": "/app/communications",
		"has_permission": "tatva_connect.api.apps.check_app_permission",
	}
]

# WATI WhatsApp — route frappe_whatsapp through WATI (never Meta): Message sends, Notification sends, Templates neutralise Meta create/edit/fetch.
override_doctype_class = {
	"WhatsApp Message": "tatva_connect.whatsapp.message.WATIWhatsAppMessage",
	"WhatsApp Notification": "tatva_connect.whatsapp.notification.WATINotification",
	"WhatsApp Templates": "tatva_connect.whatsapp.templates.WATITemplates",
	# Read offloaded file bytes from Azure Blob; no-op for local files and when the storage kill-switch is off.
	"File": "tatva_connect.storage.file_override.FileOverride",
	# Grain-gate CRM Lead assignment: a grain-tagged rule fires only on a matching-grain lead; stock otherwise.
	"Assignment Rule": "tatva_connect.lead.assignment_rule.TatvaAssignmentRule",
}

# Rewire frappe_whatsapp's "Sync templates" endpoint to pull from WATI (read-only mirror), not Meta — for the desk button and any caller.
override_whitelisted_methods = {
	"frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.fetch": "tatva_connect.whatsapp.templates_sync.sync_from_wati",
	# Acefone rides crm's NATIVE call UI (no fork): phone icon -> Acefone bridge call, call-log fetch gains a playable recording path.
	"crm.integrations.exotel.handler.make_a_call": "tatva_connect.telephony.bridge.make_a_call",
	"crm.fcrm.doctype.crm_call_log.crm_call_log.get_call_log": "tatva_connect.telephony.bridge.get_call_log",
	# Mirror LSQ: surface Task created/closed in the Lead/Deal activity timeline (native omits it); derived on read, nothing stored.
	"crm.api.activities.get_activities": "tatva_connect.api.activities.get_activities",
	# Attach the standard _link_titles map so list/Kanban cells show a Link's clean title (its
	# doctype title_field) instead of the composite :: PK. Generic; delegates to native get_data.
	"crm.api.doc.get_data": "tatva_connect.api.list_link_titles.get_data",
	# VAPT hardening — native crm methods that BYPASS the permission engine; intercept -> has_permission gate -> delegate to the unchanged native fn (no crm fork).
	"crm.api.doc.get_assigned_users": "tatva_connect.access.native_guards.get_assigned_users",
	"crm.api.doc.get_linked_docs_of_document": "tatva_connect.access.native_guards.get_linked_docs_of_document",
	"crm.integrations.api.add_task_to_call_log": "tatva_connect.access.native_guards.add_task_to_call_log",
	"crm.integrations.api.add_note_to_call_log": "tatva_connect.access.native_guards.add_note_to_call_log",
	"crm.integrations.api.get_recording_url": "tatva_connect.access.native_guards.get_recording_url",
	"crm.integrations.api.set_default_calling_medium": "tatva_connect.access.native_guards.set_default_calling_medium",
	"crm.integrations.api.get_contact_by_phone_number": "tatva_connect.access.native_guards.get_contact_by_phone_number",
	"crm.integrations.api.get_contact_lead_or_deal_from_number": "tatva_connect.access.native_guards.get_contact_lead_or_deal_from_number",
	"crm.fcrm.doctype.crm_deal.crm_deal.create_deal": "tatva_connect.access.native_guards.create_deal",
	"crm.fcrm.doctype.crm_deal.api.get_deal_contacts": "tatva_connect.access.native_guards.get_deal_contacts",
	"crm.api.whatsapp.get_whatsapp_messages": "tatva_connect.access.native_guards.get_whatsapp_messages",
}

# Smart Views — the grain surface; read-only whitelisted endpoints AND the same permission_query_conditions into every list+count (fail-closed), reading the live CRM Lead API Field catalog.

# Child doctypes have no native list scoping (crm scopes only Lead/Deal); mirror each child's parent Lead/Deal visibility onto lists + single-doc reads. Policy lives once in access/visibility.py.
permission_query_conditions = {
	"CRM Task": "tatva_connect.tasks.permissions.get_task_permission_query_conditions",
	"CRM Call Log": "tatva_connect.telephony.permissions.get_call_log_permission_query_conditions",
	"FCRM Note": "tatva_connect.notes.permissions.get_note_permission_query_conditions",
	"WhatsApp Message": "tatva_connect.whatsapp.permissions.get_whatsapp_message_permission_query_conditions",
	# Picklist Engine: clamp any generic list read of CRM Picklist Value to the caller's entitled grains, so get_list can't bypass the scoped picklist_query.
	"CRM Picklist Value": "tatva_connect.access.picklist.get_picklist_value_permission_query_conditions",
}
has_permission = {
	"CRM Task": "tatva_connect.tasks.permissions.has_task_permission",
	"CRM Call Log": "tatva_connect.telephony.permissions.has_call_log_permission",
	"FCRM Note": "tatva_connect.notes.permissions.has_note_permission",
	"WhatsApp Message": "tatva_connect.whatsapp.permissions.has_whatsapp_message_permission",
}

# Event-driven automations: each side-effect lives in its feature module; providers persist only their own records, every side-effect hangs off here.
doc_events = {
	"CRM Lead": {
		# canonicalise empty routing fields (''->None) BEFORE dedup, so the {mobile, vertical, group} anchor + stored leads agree (NULL, never '').
		"before_validate": [
			"tatva_connect.lead.leads.canonicalize_routing_fields",
		],
		# canonicalise phones (+E.164) first, then dedup on the canonical value
		"validate": [
			"tatva_connect.lead.leads.normalize_lead_phones",
			"tatva_connect.lead.leads.dedup_guard",
			"tatva_connect.lead.leads.validate_stage",
			# mirror the latest lab row's headline metrics up to the core Lead fields
			"tatva_connect.lead.leads.sync_headline_metrics",
		],
	},
	"CRM Task": {
		# seed first (fills checklist from template), then enforce (gates Done); enforce_location is the fail-closed backstop guaranteeing coords on every save path.
		"validate": [
			"tatva_connect.tasks.tasks.seed_checklist",
			"tatva_connect.tasks.tasks.enforce_checklist",
			"tatva_connect.tasks.tasks.enforce_location",
			# fail-closed: an activity task can't be marked Done with its form unfilled (any path).
			"tatva_connect.tasks.tasks.enforce_activity_logged",
		],
		# Automation engine: on the first Done flip of a lead-linked task, enqueue (after commit) every matching enabled rule (gated, fail-closed, non-re-entrant).
		# Metrics rollup: recompute the lead's count for this task's type (absolute, self-healing, gated, injection-safe).
		"on_update": [
			"tatva_connect.automation.dispatcher.fire_rules",
			"tatva_connect.tasks.metrics.refresh_for_lead",
		],
		"on_submit": [
			"tatva_connect.tasks.metrics.refresh_for_lead",
		],
		"on_cancel": [
			"tatva_connect.tasks.metrics.refresh_for_lead",
		],
		"on_trash": [
			"tatva_connect.tasks.metrics.refresh_for_lead",
		],
		# Push: ping the assignee's devices when a task lands on them (gated, enqueued).
		"after_insert": [
			"tatva_connect.notifications.events.on_task_created",
		],
	},
	"WhatsApp Message": {
		# Re-pin the account-matched lead that crm's validate clobbers to first-by-phone; runs after crm validate, before db_insert; inbound-only (flag-gated).
		"before_save": "tatva_connect.whatsapp.webhook.pin_inbound_reference",
		"after_insert": "tatva_connect.whatsapp.inbound.on_inbound_message",
	},
	# the partner-API catalog is data-driven (cached read of CRM Lead API Field); drop the cache on any catalog row change so the API picks it up at once.
	"CRM Lead API Field": {
		"on_update": "tatva_connect.api.partner.clear_catalog_cache",
		"on_trash": "tatva_connect.api.partner.clear_catalog_cache",
	},
	# Generic web-intake: a Web Form lands a submission row -> upsert a routed lead.
	"CRM Enrolment Submission": {
		"after_insert": "tatva_connect.intake.intake.process_submission",
	},
	# Per-form intake sinks are runtime custom DocTypes with no code hook — a single wildcard after_insert processes them; early-returns cheaply (cached set test) for every non-intake doctype.
	"*": {
		"after_insert": "tatva_connect.intake.intake.route_submission",
	},
	# The wildcard router's guard set is DERIVED from enabled intake forms; bust its cache on any form add/toggle/remove so it never serves a stale set.
	"CRM Intake Form": {
		# On save: scaffold/sync the per-form DocType + Web Form from the contract, then refresh the wildcard-router guard set so the new sink routes immediately.
		"on_update": [
			"tatva_connect.intake.builder.sync_form",
			"tatva_connect.intake.intake.bust_intake_doctype_cache",
		],
		"on_trash": "tatva_connect.intake.intake.bust_intake_doctype_cache",
	},
	# Lead assigned to an agent -> raise a "Call Lead" follow-up task AND push the assignment to the rep's devices (gated, enqueued).
	"ToDo": {
		"after_insert": [
			"tatva_connect.tasks.tasks.on_lead_assignment",
			"tatva_connect.notifications.events.on_lead_assigned",
		],
	},
	# Azure Blob offload: push bytes after the row + local file exist, delete the blob on File delete; gated by the CRM Azure Storage Settings kill-switch.
	"File": {
		# Screen enrolment-submission uploads beyond native checks (magic-byte + ClamAV); scoped + gated inside guard_file, no-op for every other file.
		"before_insert": "tatva_connect.intake.guards.guard_file",
		"validate": "tatva_connect.storage.file_events.apply_privacy_policy",
		"after_insert": "tatva_connect.storage.file_events.after_insert",
		"on_trash": "tatva_connect.storage.file_events.on_trash",
	},
}

# Safety-net: re-sync every WATI account's templates every 6h so the local mirror stays current (manual "Sync from WATI" stays real-time).
scheduler_events = {
	"cron": {
		# Every 6h: WATI template mirror refresh + roll the raw API/webhook log into the immortal CRM API Metric table (observability plane).
		"0 */6 * * *": [
			"tatva_connect.whatsapp.templates_sync.scheduled_sync_all",
			"tatva_connect.observability.rollup.run",
		],
		# Daily: sweep abandoned email-draft staging files.
		"30 2 * * *": ["tatva_connect.api.email.purge_draft_attachments"],
		# Daily: prune automation Run Log rows past the retention window.
		"0 3 * * *": ["tatva_connect.automation.dispatcher.sweep_run_log"],
	},
}

# Ship the CRM Form Scripts from their .js source files on every migrate — keeps them version-controlled and in sync.
after_migrate = [
	# Structural patches (indexes/Select options/custom fields); install-app baselines patches.txt WITHOUT running it, so re-run them here (idempotent). Schema before data.
	"tatva_connect.schema_setup.apply_schema",
	# Lock the stock-open doctype permission matrix on shared/core doctypes (Layer-1 VAPT fix); structural + idempotent, same reason as schema_setup above.
	"tatva_connect.access.lockdown.apply",
	# Master-data seeds run BEFORE the drift asserts below so a registry-drift throw never skips them; depend only on schema + fixtures (already applied); idempotent.
	"tatva_connect.seeds.seed_master_data",
	# Automation control plane: seed the catalog rows, then assert no doc_event/scheduler path drifts out of the registry (catalog after schema, drift after rows exist).
	"tatva_connect.automation.seed.sync_catalog",
	"tatva_connect.automation.drift.assert_registered",
	# Every notification grain must point at a real automation row (the ONE global gate); a drifting catalog fails the migrate.
	"tatva_connect.notifications.drift.assert_registered",
	# Layer-4 guard: fail the migrate if a locked doctype drifts open to All/Guest.
	"tatva_connect.access.lockdown.assert_locked",
	"tatva_connect.form_scripts_seed.seed",
	"tatva_connect.client_scripts_seed.seed",
	"tatva_connect.api.email.ensure_draft_folder",
]

# Schema-as-code: the custom_provider Select on WhatsApp Account ships as a fixture (the WATI Settings doctype ships as its own doctype JSON).
fixtures = [
	# Desk STRUCTURE (Workspace + Workspace Sidebar) is NOT fixtures — migrate's remove_orphan_entities() prunes any standard space with no backing FILE, so each ships as STANDARD FILES (model-sync auto-imports them). Only dashboard CONTENT below stays fixtures.
	# Observability dashboard records — charts/cards aren't in IMPORTABLE_DOCTYPES (no module-folder sync), so they ship as name-scoped fixtures (the Dashboard Chart SOURCE is module-standard and syncs on migrate); name-filtered so export never vacuums other apps'.
	{"dt": "Dashboard Chart", "filters": [["name", "in", [
		"API Traffic (Daily)", "API Errors (Daily)", "API Error Rate (Daily)",
		"API p95 Latency (Daily)", "API p95 Latency (Hourly)", "API Requests by Endpoint",
	]]]},
	{"dt": "Number Card", "filters": [["name", "in", [
		"API Requests (24h)", "API Errors (24h)", "API Error Rate (24h)", "API p95 Latency (24h)",
	]]]},
	{
		"dt": "Custom Field",
		# Full parity (schema-as-code): ship EVERY custom field we add to these native doctypes so a fresh migrate reproduces the entire schema; every Custom Field here is ours; workflow_state is Frappe-managed (excluded).
		"filters": [
			["dt", "in", ["CRM Lead", "CRM Task", "CRM Program", "CRM Call Log", "CRM Telephony Agent", "WhatsApp Account", "File"]],
			["fieldname", "!=", "workflow_state"],
		],
	},
	# WhatsApp Message: ship ONLY our field by name — never vacuum crm/frappe_whatsapp's own custom fields on this shared doctype.
	{"dt": "Custom Field", "filters": [["name", "in", ["WhatsApp Message-custom_failed_reason"]]]},
	# Field-property overrides on CRM data-model doctypes (option-less profile Select fields -> free-text, so form-written values store AND display).
	{"dt": "Property Setter", "filters": [["name", "in", [
		# P9: nivo_indication moved Plan -> Drug Program Profile; its free-text override follows the field (migration recreates here + drops the stale Plan ones).
		"CRM Drug Program Profile-nivo_indication-fieldtype",
		"CRM Drug Program Profile-nivo_indication-options",
		# Scoping fix: exclude the secondary (history) Link fields from User Permission matching so a scoped user is filtered by the CURRENT field only (else blank/different history HIDES valid in-scope leads).
		"CRM Lead-custom_previous_program-ignore_user_permissions",
		"CRM Lead-custom_origin_vertical-ignore_user_permissions",
		# Program is rep-editable within a line: drop its field-level lock (permlevel 1 -> 0); vertical + group stay permlevel 1 (only managers/integration move a lead between lines).
		"CRM Lead-custom_current_program-permlevel",
		# Field governance (Phase 3): clinical + patient fields are API-owned -> read-only; lead-detail fields stay writable; global default grid columns via in_list_view on child profile DocFields.
		"CRM Lead-mobile_no-read_only",
		"CRM Lead-first_name-read_only",
		"CRM Lead-last_name-read_only",
		"CRM Lead-custom_gender-read_only",
		"CRM Lead-custom_dob-read_only",
		"CRM Lead-custom_patient_id-read_only",
		"CRM Lab Profile-hba1c-read_only",
		"CRM Lab Profile-fbs-read_only",
		"CRM Lab Profile-total_cholesterol-read_only",
		"CRM Lab Profile-triglycerides-read_only",
		"CRM Lab Profile-ldl-read_only",
		"CRM Lab Profile-hdl-read_only",
		"CRM Lab Profile-vldl-read_only",
		"CRM Lab Profile-creatinine-read_only",
		"CRM Lab Profile-egfr-read_only",
		"CRM Lab Profile-alt_sgpt-read_only",
		"CRM Lab Profile-ggt-read_only",
		"CRM Lab Profile-tsh-read_only",
		"CRM Lab Profile-height_feet-read_only",
		"CRM Lab Profile-weight_kg-read_only",
		"CRM Lab Profile-report_date-read_only",
		"CRM Lab Profile-report_date-in_list_view",
		"CRM Lab Profile-hba1c-in_list_view",
		"CRM Lab Profile-fbs-in_list_view",
		"CRM Plan Profile-policy_number-in_list_view",
		"CRM Plan Profile-member_id-in_list_view",
		"CRM Plan Profile-payment_link-in_list_view",
		"CRM Plan Profile-plan_name-in_list_view",
		# WATI Bearer tokens are long JWTs (>300 chars); raise the token field's form length cap (300 -> 1000) so an operator can paste a real token (column is already TEXT — form-validation only).
		"WhatsApp Account-token-length",
		# Declutter (WATI-only): hide frappe_whatsapp's Meta-handshake fields we never use, so the form shows just our custom_webhook_token (values hidden, not dropped).
		"WhatsApp Account-webhook_verify_token-hidden",
		"WhatsApp Account-app_id-hidden",
		"WhatsApp Account-business_id-hidden",
		"WhatsApp Account-phone_id-hidden",
		# Declutter WhatsApp Notification: hide Meta media/header/button/print fields our WATI text-template path doesn't use, so the form shows just template + variable mapping + account.
		"WhatsApp Notification-code-hidden",
		"WhatsApp Notification-attach_document_print-hidden",
		"WhatsApp Notification-custom_attachment-hidden",
		"WhatsApp Notification-attach-hidden",
		"WhatsApp Notification-file_name-hidden",
		"WhatsApp Notification-header_type-hidden",
		"WhatsApp Notification-attach_from_field-hidden",
		"WhatsApp Notification-button_fields-hidden",
	]]]},
	# NOTE: only schema-as-code ships as fixtures (Custom Field columns + Property Setter overrides); business/master DATA is NOT seeded — it ships as manual db-seeds/ SQL the operator runs, so the app comes up DORMANT (CRM City is the one intrinsic exception, via seed_india_cities).
	# WhatsApp capability roles — definitions only (name-filtered so export never vacuums other roles); ship DORMANT, assigned to nobody. Operator grants them. See tatva_connect.whatsapp.roles.
	{"dt": "Role", "filters": [["name", "in", [whatsapp_roles.WHATSAPP_USER, whatsapp_roles.WHATSAPP_ADMIN]]]},
]

# WhatsApp capability policy (the ONE brain) — the crm fork's validate_access() reads this hook to
# decide who may use WhatsApp, so the allow-list lives here, not in the fork. See whatsapp/roles.py.
whatsapp_capability_roles = whatsapp_roles.CAPABILITY_ROLES

# Apps
# ------------------

# Hard deps: we override frappe_whatsapp doctypes and extend crm; declaring them enforces install order so the custom_field fixture never aborts and drops the CRM Lead fields.
required_apps = ["crm", "frappe_whatsapp"]

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "tatva_connect",
# 		"logo": "/assets/tatva_connect/logo.png",
# 		"title": "Tatva Connect",
# 		"route": "/tatva_connect",
# 		"has_permission": "tatva_connect.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/tatva_connect/css/tatva_connect.css"
# Shared Desk helpers for the webhook account forms (token generator + URL banner); both account Client Scripts call this one asset.
app_include_js = "/assets/tatva_connect/js/webhook_account_form.js"

# include js, css files in header of web template
# web_include_css = "/assets/tatva_connect/css/tatva_connect.css"
# web_include_js = "/assets/tatva_connect/js/tatva_connect.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "tatva_connect/public/scss/website"

# include js, css files in header of web form
# (none — intake bot defence is server-side rate limiting, NOT a client widget; no DOM code)
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "tatva_connect/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "tatva_connect.utils.jinja_methods",
# 	"filters": "tatva_connect.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "tatva_connect.install.before_install"
# Fresh-install master-data seeding is handled on after_migrate (tatva_connect.seeds),
# which runs after fixtures so the Linked masters exist. See seeds.py.
# after_install = "tatva_connect.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "tatva_connect.uninstall.before_uninstall"
# after_uninstall = "tatva_connect.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "tatva_connect.utils.before_app_install"
# after_app_install = "tatva_connect.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "tatva_connect.utils.before_app_uninstall"
# after_app_uninstall = "tatva_connect.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "tatva_connect.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways (the active hooks live up top, near the other overrides).

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"tatva_connect.tasks.all"
# 	],
# 	"daily": [
# 		"tatva_connect.tasks.daily"
# 	],
# 	"hourly": [
# 		"tatva_connect.tasks.hourly"
# 	],
# 	"weekly": [
# 		"tatva_connect.tasks.weekly"
# 	],
# 	"monthly": [
# 		"tatva_connect.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "tatva_connect.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "tatva_connect.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "tatva_connect.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# Observability: stamp a monotonic start on every request; the after_request logger reads it to compute latency for watched endpoints.
before_request = [
	"tatva_connect.observability.capture.stamp_start",
	# Stricter per-IP/per-phone rate limit on the enrolment web-form submit (scoped + gated inside).
	"tatva_connect.intake.guards.throttle_intake",
]
# Rewrite framework-layer errors on partner-API paths into the unified error contract, then log one raw row per partner-API/webhook hit (runs last); both no-op for other endpoints.
after_request = [
	"tatva_connect.api._base.normalise_partner_response",
	"tatva_connect.observability.capture.log_request",
]

# Job Events
# ----------
# before_job = ["tatva_connect.utils.before_job"]
# after_job = ["tatva_connect.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"tatva_connect.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

