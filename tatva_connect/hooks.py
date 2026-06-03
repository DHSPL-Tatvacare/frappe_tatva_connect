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
	"WhatsApp Message": "tatva_connect.wati.message.WATIWhatsAppMessage",
	"WhatsApp Notification": "tatva_connect.wati.notification.WATINotification",
	"WhatsApp Templates": "tatva_connect.wati.templates.WATITemplates",
}

# Rewire frappe_whatsapp's "Sync templates" endpoint to pull from WATI, not Meta.
# The desk list-view button calls this method; routing it here means that button
# (and any caller) syncs the read-only WATI mirror — never reaches Meta.
override_whitelisted_methods = {
	"frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.fetch": "tatva_connect.wati.templates_sync.sync_from_wati",
	# Acefone rides crm's NATIVE call UI (no fork): the native phone icon's call
	# method becomes an Acefone bridge call, and the call-log fetch gains a
	# playable recording path for Acefone. See tatva_connect/acefone/bridge.py.
	"crm.integrations.exotel.handler.make_a_call": "tatva_connect.acefone.bridge.make_a_call",
	"crm.fcrm.doctype.crm_call_log.crm_call_log.get_call_log": "tatva_connect.acefone.bridge.get_call_log",
}

# Event-driven automations (the single home — see tatva_connect/automation/).
# Providers only persist their own records; every side-effect hangs off here.
doc_events = {
	"CRM Lead": {
		"before_insert": "tatva_connect.automation.leads.dedup_guard",
	},
	"CRM Task": {
		# seed first (fills the checklist from the template), then enforce (gates Done)
		"validate": [
			"tatva_connect.automation.tasks.seed_checklist",
			"tatva_connect.automation.tasks.enforce_checklist",
		],
	},
	"WhatsApp Message": {
		"after_insert": "tatva_connect.automation.whatsapp.on_inbound_message",
	},
	# Generic web-intake: a Web Form lands a submission row -> upsert a routed lead.
	"CRM Enrolment Submission": {
		"after_insert": "tatva_connect.automation.intake.process_submission",
	},
}

# Safety-net: re-sync every WATI account's templates every 6 hours so the local
# mirror is almost always current (manual "Sync from WATI" stays real-time).
scheduler_events = {
	"cron": {
		"0 */6 * * *": ["tatva_connect.wati.templates_sync.scheduled_sync_all"],
	},
}

# Schema-as-code: the custom_is_wati flag on WhatsApp Account ships as a fixture
# (the WATI Settings doctype ships as its own doctype JSON in this app).
fixtures = [
	{
		"dt": "Custom Field",
		"filters": [
			[
				"name",
				"in",
				[
					"WhatsApp Account-custom_is_wati",
					"WhatsApp Account-custom_wati_channel_number",
					"CRM Telephony Agent-acefone_number",
					"CRM Call Log-custom_acefone_account",
					"CRM Task-custom_task_type",
					"CRM Task-custom_checklist",
				],
			]
		],
	},
	# The public enrolment Web Form ships as a fixture (carries its fields).
	{"dt": "Web Form", "filters": [["name", "=", "nivolumab-patient-enrolment"]]},
]

# Apps
# ------------------

# required_apps = []

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
# after_request = ["tatva_connect.utils.after_request"]

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

