app_name = "tatva_connect"
app_title = "Tatva Connect"
app_publisher = "TatvaCare"
app_description = "TatvaCare custom Frappe app: WATI WhatsApp, Acefone telephony, CRM overrides"
app_email = "pareekshith.bompally@tatvacare.in"
app_license = "mit"

# WATI WhatsApp — route frappe_whatsapp through WATI; never reach Meta.
# Seam 1: WhatsApp Message      — agent sends + CRM template picker.
# Seam 2: WhatsApp Notification — automated/scheduled sends (own Meta call).
# Templates: neutralise Meta create/edit/fetch (templates live on WATI).
override_doctype_class = {
	"WhatsApp Message": "tatva_connect.whatsapp.message.WATIWhatsAppMessage",
	"WhatsApp Notification": "tatva_connect.whatsapp.notification.WATINotification",
	"WhatsApp Templates": "tatva_connect.whatsapp.templates.WATITemplates",
}

# Rewire frappe_whatsapp's "Sync templates" endpoint to pull from WATI, not Meta.
# The desk list-view button calls this method; routing it here means that button
# (and any caller) syncs the read-only WATI mirror — never reaches Meta.
override_whitelisted_methods = {
	"frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.fetch": "tatva_connect.whatsapp.templates_sync.sync_from_wati",
	# Acefone rides crm's NATIVE call UI (no fork): the native phone icon's call
	# method becomes an Acefone bridge call, and the call-log fetch gains a
	# playable recording path for Acefone. See tatva_connect/telephony/bridge.py.
	"crm.integrations.exotel.handler.make_a_call": "tatva_connect.telephony.bridge.make_a_call",
	"crm.fcrm.doctype.crm_call_log.crm_call_log.get_call_log": "tatva_connect.telephony.bridge.get_call_log",
}

# Event-driven automations: each side-effect lives in its feature module
# (lead/, tasks/, whatsapp/, intake/) — providers only persist their own records.
# Providers only persist their own records; every side-effect hangs off here.
doc_events = {
	"CRM Lead": {
		# canonicalise empty routing fields (''->None) BEFORE dedup, so the
		# {mobile, vertical, group} anchor + stored leads agree (NULL, never '').
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
		# seed first (fills the checklist from the template), then enforce (gates Done)
		"validate": [
			"tatva_connect.tasks.tasks.seed_checklist",
			"tatva_connect.tasks.tasks.enforce_checklist",
		],
	},
	"WhatsApp Message": {
		# Re-pin the account-matched lead that crm's validate clobbers to first-by-phone.
		# Runs after crm validate, before db_insert + crm on_update. Inbound-only (flag-gated).
		"before_save": "tatva_connect.whatsapp.webhook.pin_inbound_reference",
		"after_insert": "tatva_connect.whatsapp.inbound.on_inbound_message",
	},
	# the partner-API catalog is data-driven (read from CRM Lead API Field, cached) —
	# drop the cache whenever a catalog row changes so the API picks it up at once.
	"CRM Lead API Field": {
		"on_update": "tatva_connect.api.partner.clear_catalog_cache",
		"on_trash": "tatva_connect.api.partner.clear_catalog_cache",
	},
	# Generic web-intake: a Web Form lands a submission row -> upsert a routed lead.
	"CRM Enrolment Submission": {
		"after_insert": "tatva_connect.intake.intake.process_submission",
	},
	# Lead assigned to an agent -> raise a "Call Lead" task (on-lead-create follow-up).
	"ToDo": {
		"after_insert": "tatva_connect.tasks.tasks.on_lead_assignment",
	},
}

# Safety-net: re-sync every WATI account's templates every 6 hours so the local
# mirror is almost always current (manual "Sync from WATI" stays real-time).
scheduler_events = {
	"cron": {
		"0 */6 * * *": ["tatva_connect.whatsapp.templates_sync.scheduled_sync_all"],
	},
}

# Ship the CRM Form Scripts (WATI send-template + WhatsApp UI gate) from their .js
# source files on every migrate — keeps them version-controlled and in sync.
after_migrate = [
	"tatva_connect.form_scripts_seed.seed",
	# Master-data seeds: install-app baselines patches.txt without running it, so seed
	# here (idempotent; runs after fixtures so Linked masters exist; safe on every migrate).
	"tatva_connect.seeds.seed_master_data",
]

# Schema-as-code: the custom_is_wati flag on WhatsApp Account ships as a fixture
# (the WATI Settings doctype ships as its own doctype JSON in this app).
fixtures = [
	{
		"dt": "Custom Field",
		# Full parity (schema-as-code): ship EVERY custom field we add to native doctypes,
		# not a curated subset, so a fresh `bench migrate` reproduces the ENTIRE Lead schema
		# (all 42 CRM Lead fields incl. routing/stage/headline/footprint/child-links) on a
		# clean server. Every Custom Field on these doctypes is ours (modules own native
		# fields in JSON, not as Custom Fields); workflow_state is Frappe-managed (excluded).
		"filters": [
			["dt", "in", ["CRM Lead", "CRM Task", "CRM Call Log", "CRM Telephony Agent", "WhatsApp Account"]],
			["fieldname", "!=", "workflow_state"],
		],
	},
	# The public enrolment Web Form ships as a fixture (carries its fields).
	{"dt": "Web Form", "filters": [["name", "=", "nivolumab-patient-enrolment"]]},
	# CRM Lead layouts (side panel / quick entry / grid rows) are currently live-only
	# DB records — capture them so they're reproducible. `bench export-fixtures` writes
	# the live JSON here; the group-layout repoint patch (fix_group_layout_slot) runs on
	# migrate BEFORE the export so the captured side-panel/quick-entry use custom_group,
	# not the dead custom_psp_group. (Phase 3.)
	{"dt": "CRM Fields Layout", "filters": [["name", "in", [
		"CRM Lead-Side Panel",
		"CRM Lead-Quick Entry",
		# The Data tab (profile child-tables + clinical sections). Captured so the
		# section structure ships with the app — incl. the P9 Acquisition + Drug
		# Program sections the program gate (lead_data_tab_gate.js) shows/hides. (P10.)
		"CRM Lead-Data Fields",
	]]]},
	# Field-property overrides on CRM data-model doctypes (profile Select fields with
	# no options -> free-text, so form-written values both store AND display).
	{"dt": "Property Setter", "filters": [["name", "in", [
		# P9: nivo_indication moved Plan -> Drug Program Profile; its free-text override
		# follows the field (the migration recreates these on the new doctype + drops the stale Plan ones).
		"CRM Drug Program Profile-nivo_indication-fieldtype",
		"CRM Drug Program Profile-nivo_indication-options",
		# Scoping fix: exclude the secondary (history) Link fields from User Permission
		# matching, so a Program/Vertical-scoped user is filtered by the CURRENT field
		# only. Without this, a blank/different previous_program/origin_vertical fails the
		# match under strict perms and HIDES valid in-scope leads. See docs/plans.
		"CRM Lead-custom_previous_program-ignore_user_permissions",
		"CRM Lead-custom_origin_vertical-ignore_user_permissions",
		# Program is rep-editable within a line: drop its field-level lock (permlevel 1 -> 0).
		# Vertical + group stay permlevel 1 (only managers/integration move a lead between lines).
		"CRM Lead-custom_current_program-permlevel",
		# Field governance (Phase 3): clinical + patient fields are API-owned -> read-only
		# (agents can't hand-edit). Lead-detail fields stay writable. Priority Text is
		# admin-only (permlevel 1). Global default grid columns via in_list_view on the
		# child profile DocFields (NOT the per-user gear).
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
	]]]},
	# Master data (routing taxonomy): the Link targets for the CRM Lead routing fields
	# (custom_vertical / custom_group / custom_current_program). Seeded so a fresh prod has
	# the masters the leads reference — without these the Link fields point at empty doctypes.
	# Simple field-named masters, no inter-deps. (CRM Lead Stage is seeded via seed_lead_stages;
	# CRM City via seed_india_cities.)
	{"dt": "CRM Vertical"},
	{"dt": "CRM Group"},
	{"dt": "CRM Program"},
]

# Apps
# ------------------

# Hard deps: we override frappe_whatsapp doctypes and extend crm. Declaring them
# enforces install order so the custom_field.json fixture (which has WhatsApp Account
# fields) never aborts and silently drops the CRM Lead fields with it.
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
# app_include_js = "/assets/tatva_connect/js/tatva_connect.js"

# include js, css files in header of web template
# web_include_css = "/assets/tatva_connect/css/tatva_connect.css"
# web_include_js = "/assets/tatva_connect/js/tatva_connect.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "tatva_connect/public/scss/website"

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
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

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
# before_request = ["tatva_connect.utils.before_request"]
# Rewrite framework-layer errors (bad key / malformed body / not-whitelisted) on
# partner-API paths into the unified {status:error, error:{code,message}} contract.
after_request = ["tatva_connect.api.partner.normalise_partner_response"]

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

