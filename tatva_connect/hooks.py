from tatva_connect.whatsapp import roles as whatsapp_roles
from tatva_connect.workflow_engine import thresholds as workflow_thresholds

app_name = "tatva_connect"
app_title = "Tatva Connect"
app_publisher = "TatvaCare"
app_description = "Backend for TatvaCare CRM — doctypes, APIs, integrations, and business logic. Part of TatvaCare One."
app_email = "pareekshith.bompally@tatvacare.in"
app_license = "AGPLv3"

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

# WhatsApp channel — route frappe_whatsapp through the account's own channel adapter (never Meta): Message sends, Notification sends, Templates neutralise Meta create/edit/fetch.
override_doctype_class = {
	"WhatsApp Message": "tatva_connect.whatsapp.message.ChannelWhatsAppMessage",
	"WhatsApp Notification": "tatva_connect.whatsapp.notification.ChannelWhatsAppNotification",
	"WhatsApp Templates": "tatva_connect.whatsapp.templates.ChannelWhatsAppTemplates",
	# Read offloaded file bytes from Azure Blob; no-op for local files and when the storage kill-switch is off.
	"File": "tatva_connect.storage.file_override.FileOverride",
	# Grain-gate CRM Lead assignment: a grain-tagged rule fires only on a matching-grain lead; stock otherwise.
	"Assignment Rule": "tatva_connect.lead.assignment_rule.TatvaAssignmentRule",
	# The crm SPA's own dashboard, carrying the role/cards custom fields — one dashboard doctype, not a second beside it.
	"CRM Dashboard": "tatva_connect.dashboard.overrides.CRMDashboardOverride",
	# An Insights invite may only reach an existing enabled login; upstream mints a User for ANY address and logs it in from the link.
	"Insights User Invitation": "tatva_connect.access.insights_invitation.TatvaInsightsUserInvitation",
	# Facebook discovery/crawl through our Graph layer: Meta's reason surfaces, no silent empty, no token in logs.
	"Lead Sync Source": "tatva_connect.lead_sync.source.TatvaLeadSyncSource",
	# A question maps to a catalog field_key, checked against the form's contract; upstream compares bare fieldnames and throws on every edit.
	"Facebook Lead Form": "tatva_connect.lead_sync.form.TatvaFacebookLeadForm",
	# A retry re-fetches the lead from Meta and folds it through the contract; upstream replays a payload we no longer keep, through a class that reads no grain.
	"Failed Lead Sync Log": "tatva_connect.lead_sync.failure_log.TatvaFailedLeadSyncLog",
	# Webhook ingress: derive the indexed token digest and refuse a config that would reject every
	# call. Auth is infrastructure, never a toggleable automation, so it is bound here rather than
	# in doc_events. CRM Telephony Account gets the same two calls from its own controller.
	"WhatsApp Account": "tatva_connect.whatsapp.account.ChannelWhatsAppAccount",
	# Mask secrets on every Error Log row, whichever app wrote it: frappe's own make_request logs the
	# failing URL before our handler runs. Infrastructure, never a toggleable automation, hence bound here.
	"Error Log": "tatva_connect.observability.error_log.MaskedErrorLog",
	# Audit Aug'26 F8 (IDOR): `member` names the SUBJECT and arrives from the request, while if_owner only guards the row the forger already owns. A controller, not a doc_event, because lms's own before_insert validates duplicates and eligibility against `member` and every hook lands after it (document.py:1580).
	"LMS Enrollment": "tatva_connect.access.lms_enrollment.TatvaLMSEnrollment",
	# The five listing declarations are ours, so they live here: get_controller is the ONE function the list payload, the saved-view seeder and the rep pickers all already call.
	"CRM Lead": "tatva_connect.list_engine.columns.TatvaCRMLead",
	"CRM Task": "tatva_connect.list_engine.columns.TatvaCRMTask",
	"CRM Call Log": "tatva_connect.list_engine.columns.TatvaCRMCallLog",
	"FCRM Note": "tatva_connect.list_engine.columns.TatvaFCRMNote",
	"CRM Deal": "tatva_connect.list_engine.columns.TatvaCRMDeal",
	# A profile link reaches the DOM as an href, so its scheme is judged at write time; infrastructure, never a toggleable automation, hence bound here.
	"User": "tatva_connect.access.user_links.TatvaUser",
}

# Rewire frappe_whatsapp's "Sync templates" endpoint to pull from the account's provider (read-only mirror), not Meta — for the desk button and any caller.
override_whitelisted_methods = {
	# Guest doorman over frappe's upload endpoint; BOTH spellings frappe resolves to the same function (dotted via get_attr, bare via globals()) — the web form posts each in one submission.
	"upload_file": "tatva_connect.intake.guards.upload_file",
	"frappe.handler.upload_file": "tatva_connect.intake.guards.upload_file",
	"frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.fetch": "tatva_connect.whatsapp.templates_sync.sync_templates",
	# The call UI calls tatva_connect.telephony.bridge.make_a_call directly, so crm's Exotel alias is gone; this one stays for the recording player.
	"crm.fcrm.doctype.crm_call_log.crm_call_log.get_call_log": "tatva_connect.telephony.bridge.get_call_log",
	# Mirror LSQ: surface Task created/closed in the Lead/Deal activity timeline (native omits it); derived on read, nothing stored.
	"crm.api.activities.get_activities": "tatva_connect.api.activities.get_activities",
	# The dashboard a ROLE is shown, not the one a person authored: declared cards through one gated get_list door.
	"crm.api.dashboard.get_dashboard": "tatva_connect.dashboard.api.get_dashboard",
	# Attach the standard _link_titles map so list/Kanban cells show a Link's clean title (its
	# doctype title_field) instead of the composite :: PK. Generic; delegates to native get_data.
	"crm.api.doc.get_data": "tatva_connect.api.list_link_titles.get_data",
	# Honour frappe's own `max_report_rows`, which frappe declares in System Settings and enforces nowhere on the server.
	"frappe.desk.reportview.export_query": "tatva_connect.api.list_export.export_query",
	# The CRM Task list lenses resolve through CRM Task.default_list_data() so no slot or operational column reaches a rep picker; every other doctype delegates to native untouched.
	"crm.api.doc.get_filterable_fields": "tatva_connect.api.task_lenses.get_filterable_fields",
	"crm.api.doc.get_group_by_fields": "tatva_connect.api.task_lenses.get_group_by_fields",
	"crm.api.doc.sort_options": "tatva_connect.api.task_lenses.sort_options",
	# The fifth field menu; it reads doctype meta directly, so a derived field reaches it only through here — at the position the rep's stored choice gives it, never appended.
	"crm.api.doc.get_quick_filters": "tatva_connect.api.task_lenses.get_quick_filters",
	# Choosing that field records the choice natively but writes no in_standard_filter Property Setter — there is no DocField for one to describe.
	"crm.api.doc.update_quick_filters": "tatva_connect.api.task_lenses.update_quick_filters",
	# The vite door to the boot bag; the rendered-page door is `update_website_context` below. A rep's field menus are cached with no expiry, so the declaration version is what retires them when an operator authors a field.
	"crm.www.crm.get_context_for_dev": "tatva_connect.api.boot.get_context_for_dev",
	# Saving a kanban board grouped by a derived field: native resolves its columns through frappe.get_meta, which has never heard of one; a real column_field reaches native untouched.
	"crm.fcrm.doctype.crm_view_settings.crm_view_settings.create": "tatva_connect.list_engine.views.create",
	"crm.fcrm.doctype.crm_view_settings.crm_view_settings.create_or_update_standard_view": "tatva_connect.list_engine.views.create_or_update_standard_view",
	"crm.fcrm.doctype.crm_view_settings.crm_view_settings.fetch_and_update_kanban_columns": "tatva_connect.list_engine.views.fetch_and_update_kanban_columns",
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
	"crm.api.assignment_rule.get_assignment_rules_list": "tatva_connect.access.native_guards.get_assignment_rules_list",
	"crm.api.views.get_views": "tatva_connect.access.native_guards.get_views",
	# VAPT hardening — Helpdesk (agent-only internal): the KB stats endpoint bypasses the engine (S.7).
	"helpdesk.api.article.get_article_stats": "tatva_connect.access.native_guards.get_article_stats",
	# VAPT hardening — Wiki (internal handbook): legacy page history is allow_guest and reads through a
	# permission-bypassing query, so the doctype matrix cannot reach it; the wrapper gates on the page.
	"wiki.wiki.doctype.wiki_page_revision.wiki_page_revision.get_revisions": "tatva_connect.access.native_guards.get_revisions",
	# VAPT hardening — Insights (reads the site DB): the guest doc-method door runs with permissions off
	# for a published dashboard; the wrapper strips the arg that rewinds a query to its unfiltered source.
	"insights.api.run_doc_method": "tatva_connect.access.native_guards.run_doc_method",
	# Insights spreadsheet import is off: client-named tables overwrite each other and the upload leaves a File nothing owns. The file layer is untouched.
	"insights.api.get_file_data": "tatva_connect.access.insights_uploads.get_file_data",
	"insights.api.import_csv_data": "tatva_connect.access.insights_uploads.import_csv_data",
	"insights.api.data_sources.get_columns_from_uploaded_file": "tatva_connect.access.insights_uploads.get_columns_from_uploaded_file",
	"insights.api.data_sources.import_csv": "tatva_connect.access.insights_uploads.import_csv",
	# VAPT hardening — LMS (internal training, Mode 2): allow_guest + engine-bypass catalog reads; the
	# wrapper NARROWS a non-privileged caller to what they are IN (access/lms_visibility.py) and strips
	# the creator email from job details. Can't be locked via DocPerm (methods bypass the engine).
	"lms.lms.utils.get_courses": "tatva_connect.access.native_guards.get_courses",
	# Home 'my courses' falls back to featured/popular (ALL published) when the caller has none — scope it to membership so home matches the Courses list.
	"lms.lms.api.get_my_courses": "tatva_connect.access.native_guards.get_my_courses",
	# Same fallback: home 'my batches' -> get_upcoming_batches (ALL published) when the caller has none — scope to membership.
	"lms.lms.api.get_my_batches": "tatva_connect.access.native_guards.get_my_batches",
	"lms.lms.utils.get_batches": "tatva_connect.access.native_guards.get_batches",
	"lms.lms.utils.get_programs": "tatva_connect.access.native_guards.get_programs",
	"lms.lms.api.get_job_details": "tatva_connect.access.native_guards.get_job_details",
	# VAPT Aug'26: the certified-participant directory is cross-member PII (name/username/avatar/open_to) readable by any login; internal training staff only.
	"lms.lms.api.get_certified_participants": "tatva_connect.access.native_guards.get_certified_participants",
	# Audit Aug'26 F2: get_reviews has NO gate of any kind and returns each reviewer's name and avatar.
	"lms.lms.utils.get_reviews": "tatva_connect.access.native_guards.get_reviews",
	# Audit Aug'26 F3: the outline of any course was readable by any login. The guard also carries the
	# upstream-race shim (learning/outline.py) that recovers a missing course from the Referer.
	"lms.lms.utils.get_course_outline": "tatva_connect.access.native_guards.get_course_outline",
	# Audit Aug'26 F6: check_answer is the one quiz endpoint that never asks can_access_quiz, so the key
	# was walkable option-by-option; the guard adds course membership AND "you are taking it right now".
	"lms.lms.doctype.lms_quiz.lms_quiz.check_answer": "tatva_connect.access.native_guards.check_answer",
	# The lesson editor drops any block type it cannot represent, so an embed must never be STORED — the reader is served one instead, the way get_lesson already rewrites private media URLs.
	"lms.lms.utils.get_lesson": "tatva_connect.learning.embeds.get_lesson",
	# VAPT Jul — quiz assessment integrity: submit_quiz gets an atomic single-attempt guard (N2 race) +
	# a best-effort server-side timer (N6); get_quiz_with_questions stamps the open time the timer reads.
	"lms.lms.doctype.lms_quiz.lms_quiz.submit_quiz": "tatva_connect.access.native_guards.submit_quiz",
	"lms.lms.utils.get_quiz_with_questions": "tatva_connect.access.native_guards.get_quiz_with_questions",
	# A CRM Task Type carrying disable_bulk_complete refuses the list's bulk complete AT THE ENTRY POINT — core's _bulk_action swallows a per-doc validate throw into `failed` and the rep still sees success.
	"frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs": "tatva_connect.tasks.tasks.submit_cancel_or_update_docs",
	# Grain is an entitlement, not a free pick: the Create Lead form hides its three axis fields server-side (GrainSelect stamps them) and hides a section left with nothing visible.
	"crm.fcrm.doctype.crm_fields_layout.crm_fields_layout.get_fields_layout": "tatva_connect.lead.quick_entry.get_fields_layout",
	"crm.fcrm.doctype.crm_fields_layout.crm_fields_layout.save_fields_layout": "tatva_connect.lead.quick_entry.save_fields_layout",
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
	# The Definition is scoped by the grain it DECLARES, not through a parent lead — it has none. Who may see a workflow is a different question from whose leads it acts on; the engine reads workflows with get_all and is untouched by this.
	"CRM Workflow": "tatva_connect.workflow_engine.permissions.get_workflow_permission_query_conditions",
	"CRM Workflow Journey": "tatva_connect.workflow_engine.permissions.get_journey_permission_query_conditions",
	"CRM Workflow Signal": "tatva_connect.workflow_engine.permissions.get_signal_permission_query_conditions",
	"CRM Workflow Step Log": "tatva_connect.workflow_engine.permissions.get_step_log_permission_query_conditions",
	# Smart Views: restrictive backstop — the SAME predicate the SPA endpoints grant through (smartview/permissions.py), so Desk can never see more than the app door.
	"CRM Smart View": "tatva_connect.smartview.permissions.get_smart_view_permission_query_conditions",
	# LMS (audit Aug'26 F1/F4/F5/F7/F9): internal training is membership-scoped, never `published`. These
	# three ALSO close the child-table reads — frappe resolves a Batch Course / LMS Program Member /
	# LMS Program Course list against the PARENT and applies the parent's conditions (database/query.py:275,
	# 1547-1558) — and the quiz COUNT, which wraps the same filtered subquery (desk/reportview.py:70).
	"LMS Batch": "tatva_connect.access.lms_permissions.get_batch_permission_query_conditions",
	"LMS Program": "tatva_connect.access.lms_permissions.get_program_permission_query_conditions",
	"LMS Quiz": "tatva_connect.access.lms_permissions.get_quiz_permission_query_conditions",
}
has_permission = {
	"CRM Task": "tatva_connect.tasks.permissions.has_task_permission",
	"CRM Call Log": "tatva_connect.telephony.permissions.has_call_log_permission",
	"CRM Workflow": "tatva_connect.workflow_engine.permissions.has_workflow_permission",
	"CRM Workflow Journey": "tatva_connect.workflow_engine.permissions.has_journey_permission",
	"CRM Workflow Signal": "tatva_connect.workflow_engine.permissions.has_signal_permission",
	"CRM Workflow Step Log": "tatva_connect.workflow_engine.permissions.has_step_log_permission",
	"FCRM Note": "tatva_connect.notes.permissions.has_note_permission",
	"WhatsApp Message": "tatva_connect.whatsapp.permissions.has_whatsapp_message_permission",
	# Smart Views: deny-only backstop; never denies an operator or a DocShare recipient (controllers run BEFORE the share fallback).
	"CRM Smart View": "tatva_connect.smartview.permissions.has_smart_view_permission",
	# LMS: the single-doc twin of the conditions above — frappe.client.get reads through has_permission, lists do not.
	"LMS Batch": "tatva_connect.access.lms_permissions.has_batch_permission",
	"LMS Program": "tatva_connect.access.lms_permissions.has_program_permission",
	"LMS Quiz": "tatva_connect.access.lms_permissions.has_quiz_permission",
}

# Global spotlight search — a native Frappe FTS5 search class. List-valued hook: this ADDS our class
# alongside helpdesk's and wiki's, each writing its own index db. Dormant until CRM Search Settings is on.
sqlite_search = ["tatva_connect.search.index.CRMLeadSearch"]

# Event-driven automations: each side-effect lives in its feature module; providers persist only their own records, every side-effect hangs off here.
doc_events = {
	# A finished lead_import job stamps its outcome back onto the CRM Lead Import it came from.
	"CRM Bulk Job": {"on_update": "tatva_connect.lead_import.api.follow_job_status"},
	# Ownership-spoof IDOR: these LMS doctypes ship a `pass` controller, so client.insert trusts `member`; pin it to the caller. Add a doctype here to close it. LMS Enrollment is cured by its own class override above.
	"LMS Batch Feedback": {"validate": "tatva_connect.access.lms_member_guard.enforce_member"},
	"LMS Lesson Note": {"validate": "tatva_connect.access.lms_member_guard.enforce_member"},
	"LMS Programming Exercise Submission": {"validate": "tatva_connect.access.lms_member_guard.enforce_member"},
	"LMS Video Watch Duration": {"validate": "tatva_connect.access.lms_member_guard.enforce_member"},
	"CRM Lead": {
		# stamp/clamp the lead's grain from the acting user's entitlement (single->auto, manager->validated pick),
		# THEN canonicalise empty routing fields (''->None) BEFORE dedup, so the {mobile, vertical, group} anchor + stored leads agree (NULL, never '').
		"before_validate": [
			"tatva_connect.lead.leads.stamp_entitled_grain",
			"tatva_connect.lead.leads.canonicalize_routing_fields",
		],
		# canonicalise phones (+E.164) first, then dedup on the canonical value
		"validate": [
			"tatva_connect.lead.leads.normalize_lead_phones",
			"tatva_connect.lead.leads.dedup_guard",
			"tatva_connect.lead.leads.validate_stage",
			# mirror the latest lab row's headline metrics up to the core Lead fields
			"tatva_connect.lead.leads.sync_headline_metrics",
			# a user-facing field rendered as a link/redirect may only carry https:// — changed values only
			"tatva_connect.access.link_scheme.guard_link_schemes",
		],
		"on_update": [
			# tell the rep the lead is assigned to that its stage moved (fires only on the save that moved it)
			"tatva_connect.notifications.events.on_lead_stage_changed",
			# the spotlight index denormalises the lead's owner into a permission column; restamp it + its child rows
			"tatva_connect.search.index.reindex_on_lead_context_change",
			# a lead that changed grain is a different population: its journeys end rather than carry on frozen against a grain it no longer has
			"tatva_connect.workflow_engine.triggers.on_lead_grain_changed",
		],
		# The lead is going, so every journey about it ends with it. Runs BEFORE the wildcard on_trash (doctype hooks compose first, document.py:1598), so a Deleted-entry workflow starting on this delete survives it.
		"on_trash": [
			"tatva_connect.workflow_engine.triggers.on_lead_deleted",
			# A generated document is about this person, so it dies with them — and only the document path reclaims its blob (M1).
			"tatva_connect.tatva_connect.doctype.crm_campaign_document.crm_campaign_document.drop_for_lead",
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
		# Metrics rollup: recompute the lead's count for this task's type (absolute, self-healing, gated, injection-safe).
		# (Automation engine fires from the wildcard router below - doc_events["*"] - not a per-doctype hook.)
		"on_update": [
			"tatva_connect.tasks.metrics.refresh_for_lead",
			# Review flow: copy a Document Review task's Approved/Rejected verdict onto its File (badge).
			"tatva_connect.tasks.review_mirror.mirror_review_outcome",
		],
		"on_submit": [
			"tatva_connect.tasks.metrics.refresh_for_lead",
		],
		"on_cancel": [
			"tatva_connect.tasks.metrics.refresh_for_lead",
		],
		"on_trash": [
			"tatva_connect.tasks.metrics.refresh_for_lead",
			"tatva_connect.activity.timeline.drop_event",
		],
		# Push: ping the assignee's devices when a task lands on them (gated, enqueued).
		"after_insert": [
			"tatva_connect.notifications.events.on_task_created",
			"tatva_connect.activity.timeline.index_event",
		],
	},
	"WhatsApp Message": {
		# Re-pin the account-matched lead that crm's validate clobbers to first-by-phone; runs after crm validate, before db_insert; inbound-only (flag-gated).
		"before_save": "tatva_connect.whatsapp.webhook.pin_inbound_reference",
		# tell the rep a patient replied (crm writes the tray row itself; this adds only the live channel)
		"after_insert": "tatva_connect.notifications.events.on_whatsapp_received",
		# The inbound follow-up task is RETIRED here — WhatsApp Message is now an automation subject, so
		# the follow-up is a user-built rule (On WhatsApp Message Created → Create Task). The wildcard
		# router below carries the after_insert; no per-message code side-effect remains.
	},
	# the partner-API catalog is data-driven (cached read of CRM Lead API Field); drop the cache on any catalog row change so the API picks it up at once.
	"CRM Lead API Field": {
		"on_update": "tatva_connect.api.partner.clear_catalog_cache",
		"on_trash": "tatva_connect.api.partner.clear_catalog_cache",
	},
	# SSRF-guard a partner's bulk-job completion webhook URL (scoped to CRM Bulk Job webhooks only).
	"Webhook": {
		"validate": "tatva_connect.api.partner_bulk_job.guard_webhook_url",
	},
	# URL scheme safety: a user-facing field rendered as a link/redirect may only carry https://. Guarded at write time (validate), only changed values, so a legacy row saved for an unrelated reason is never blocked.
	# CRM Lead and CRM Intake Form carry this guard inside their OWN blocks above/below — a second entry keyed by the same doctype does not merge, it SHADOWS, and Python keeps the last one silently.
	"CRM Deal": {
		"validate": "tatva_connect.access.link_scheme.guard_link_schemes",
	},
	"CRM Organization": {
		"validate": "tatva_connect.access.link_scheme.guard_link_schemes",
	},
	# osm_tile_url is rendered as the map tile src; operator-set, https-only. New key (no existing CRM Maps Settings block, so nothing to shadow).
	"CRM Maps Settings": {
		"validate": "tatva_connect.access.link_scheme.guard_link_schemes",
	},
	# Per-form intake sinks are runtime custom DocTypes with no code hook — a single wildcard after_insert processes them; early-returns cheaply (cached set test) for every non-intake doctype.
	# Automation engine (Task 4): the unified (on_doctype, event) router rides the SAME wildcard - no per-doctype code push. A doctype is "live" for automation only because an enabled rule names it (router.live_doctypes, self-healing cache); every handler early-returns cheaply otherwise.
	# Automation engine (Task 10): Deleted rides on_trash - the row still exists there (before removal),
	# so router.on_deleted captures subject + context synchronously; the effect lane still runs
	# after-commit like Created/Updated (router.py's on_deleted docstring has the full nuance).
	"*": {
		# Frappe's own XSS filter skips a tag that never closes (html_utils.py:162); this re-runs it without that skip.
		"validate": [
			"tatva_connect.access.xss_guard.sanitize_unterminated_tags",
		],
		"after_insert": [
			"tatva_connect.intake.intake.route_submission",
			# Workflow engine: run/start a Flow when a Created-entry Definition's grain + When match.
			"tatva_connect.workflow_engine.triggers.on_created",
		],
		"on_update": [
			# Bond an offloaded file to the record whose Attach field names it — core's linker skips remote URLs.
			"tatva_connect.storage.file_events.link_attach_fields",
			"tatva_connect.workflow_engine.triggers.on_updated",
			# Workflow engine: a CRM Task reaching a terminal status emits its outcome, correlated by the token its node stamped; dormant + in_workflow-guarded, cheap early-return otherwise.
			"tatva_connect.workflow_engine.triggers.on_task_done",
		],
		"on_trash": [
			"tatva_connect.workflow_engine.triggers.on_trash",
		],
	},
	# The wildcard router's guard set is DERIVED from enabled intake forms; bust its cache on any form add/toggle/remove so it never serves a stale set.
	"CRM Intake Form": {
		# URL scheme safety, declared HERE and not in the link_scheme group — this doctype already owns a block, and a second one keyed the same shadows it.
		"validate": "tatva_connect.access.link_scheme.guard_link_schemes",
		# On save: scaffold/sync the per-form DocType + Web Form from the contract, then refresh the wildcard-router guard set so the new sink routes immediately.
		"on_update": [
			"tatva_connect.intake.builder.sync_form",
			"tatva_connect.intake.intake.bust_intake_doctype_cache",
		],
		"on_trash": "tatva_connect.intake.intake.bust_intake_doctype_cache",
	},
	"CRM Call Log": {
		# tell the rep an inbound call went unanswered (only the save that moves the status notifies)
		"on_update": "tatva_connect.notifications.events.on_call_missed",
		"after_insert": "tatva_connect.activity.timeline.index_event",
		# The media row is a POINTER to this call's artifacts, so it dies with the call; the File and its blob are reclaimed by the call's own attachment cleanup (M1), never from here.
		"on_trash": [
			"tatva_connect.activity.timeline.drop_event",
			"tatva_connect.storage.call_media.drop_for_call",
		],
	},
	# Lead assigned to an agent -> raise a "Call Lead" follow-up task AND push the assignment to the rep's devices (gated, enqueued).
	"ToDo": {
		"after_insert": [
			"tatva_connect.tasks.tasks.on_lead_assignment",
			"tatva_connect.notifications.events.on_lead_assigned",
			# assignment is the second leg of the lead visibility predicate the spotlight index denormalises
			"tatva_connect.search.index.reindex_on_assignment",
		],
		"on_update": "tatva_connect.search.index.reindex_on_assignment_change",
		"on_trash": "tatva_connect.search.index.reindex_on_assignment",
	},
	# crm shares a lead with its assigned agent, and a share is a row-level grant the spotlight index must carry.
	"DocShare": {
		"after_insert": "tatva_connect.search.index.reindex_on_share",
		"on_update": "tatva_connect.search.index.reindex_on_share",
		"on_trash": "tatva_connect.search.index.reindex_on_share",
	},
	# Azure Blob offload: push bytes after the row + local file exist, delete the blob on File delete; gated by the CRM Azure Storage Settings kill-switch.
	"File": {
		# Privacy + screening are NOT here on purpose: a doc_event runs after the controller, i.e. after core has already written the bytes — both live in FileOverride.before_insert.
		"after_insert": [
			"tatva_connect.storage.file_events.after_insert",
			"tatva_connect.activity.timeline.index_event",
		],
		"on_trash": [
			"tatva_connect.storage.file_events.on_trash",
			"tatva_connect.activity.timeline.drop_event",
		],
	},
	# The lead Activity rail's index — one pointer row per thing that happened, so the rail is ONE seek
	# instead of a read-time merge across six tables that grows a leg with every new type. A pointer, never
	# a copy: content is hydrated from the source row, so only insert and delete are events. What a rail
	# event IS lives once, in timeline.event_row; these lines only say which doctypes feed it.
	"FCRM Note": {
		"after_insert": "tatva_connect.activity.timeline.index_event",
		"on_trash": "tatva_connect.activity.timeline.drop_event",
	},
	"Comment": {
		"after_insert": "tatva_connect.activity.timeline.index_event",
		"on_trash": "tatva_connect.activity.timeline.drop_event",
	},
	"Communication": {
		"after_insert": "tatva_connect.activity.timeline.index_event",
		"on_trash": "tatva_connect.activity.timeline.drop_event",
	},
}

# Safety-net: re-sync every account's templates every 6h so the local mirror stays current (the manual Sync button stays real-time).
scheduler_events = {
	"cron": {
		# Every 6h: template mirror refresh + roll the raw API/webhook log into the immortal CRM API Metric table (observability plane).
		"0 */6 * * *": [
			"tatva_connect.whatsapp.templates_sync.scheduled_sync_all",
			"tatva_connect.observability.rollup.run",
		],
		# Every 15 min, OFFSET off the quarter-hour: re-ask for media a message is still owed, on a widening backoff (dormant — gated on WhatsApp::Channel::media-retry). A bare `*/15` would collide with workflow_thresholds.SWEEP_CRON, and a duplicate dict key silently deletes whichever entry is written first.
		"5,20,35,50 * * * *": ["tatva_connect.whatsapp.media_retry.sweep"],
		# Daily: sweep abandoned email-draft staging files.
		"30 2 * * *": ["tatva_connect.api.email.purge_draft_attachments"],
		# Hourly: delete Guest-uploaded intake files never bonded to a record, past the 30-min TTL (dormant — gated on Intake::RateLimit::enforcement); bounded batch, drains across ticks.
		"25 * * * *": ["tatva_connect.intake.guards.reap_guest_orphans"],
		# Daily: trim logs/monitor.json.log — the one log frappe appends to without rotating (1 GB or 30 days, whichever first).
		"30 3 * * *": ["tatva_connect.observability.monitor_log.sweep"],
		# Daily: drop expired partner-API idempotency records.
		"0 4 * * *": ["tatva_connect.api._base.purge_idempotency_keys"],
		# Hourly: fail any async bulk job stranded InProgress past its worker timeout (worker died); and drop a search index that can no longer be READ, which is the one damaged state frappe's own 3-hourly check cannot see (it asks whether the file exists, not whether it opens).
		"45 * * * *": [
			"tatva_connect.api.partner_bulk_worker.reap_stranded_jobs",
			"tatva_connect.search.index.sweep_index_health",
		],
		# Daily: purge finished async bulk jobs + results + payload past the retention window.
		"15 4 * * *": ["tatva_connect.api.partner_bulk_job.purge_expired_jobs"],
		# Every 15 min: wake due-timer Flow Instances + reconcile lost wakeups (F5); chase up voice calls whose outcome webhook never arrived (dormant — gated on AI Voice::Channel::reconcile). Cadence DECLARED in workflow_engine.thresholds (W4.3), never restated here.
		workflow_thresholds.SWEEP_CRON: [
			"tatva_connect.workflow_engine.wakeups.sweep",
			"tatva_connect.voice.reconcile.sweep",
			"tatva_connect.workflow_engine.drain.sweep",
			"tatva_connect.storage.call_media.sweep",
		],
		# Every 5 min: warn about a task falling due, and tell a rep about one already overdue (the operator's lead time goes as low as 5 min; both switches are read per pass).
		"*/5 * * * *": [
			"tatva_connect.notifications.events.sweep_due_soon",
			"tatva_connect.notifications.events.sweep_overdue",
		],
		# Nightly: re-read Facebook Pages and lead forms, so a newly published form and a changed question set are both picked up without a button press.
		"0 1 * * *": ["tatva_connect.lead_sync.discovery.refresh_all_sources"],
	},
}

# Ship the CRM Form Scripts from their .js source files on every migrate — keeps them version-controlled and in sync.
after_migrate = [
	# FIRST: field_target routes through these rows, and apply_schema below runs steps that ask it. Seeded ahead of everything so no step ever sees an empty catalog. Its own inputs are CRM Task columns, which sync_fixtures has already landed by now.
	"tatva_connect.taxonomy.task_field_seed.ensure_rows",
	# BEFORE apply_schema, not after: add_task_answer_question_index reads the key-value CRM Task Section rows to know which tables to index, so seeded later it found none on a fresh site and the index landed a whole migrate late. Depends only on the doctype and the fixture Table fields on CRM Task, both present by now on either path.
	"tatva_connect.taxonomy.task_section_seed.ensure_rows",
	# Structural patches (indexes/Select options/custom fields); install-app baselines patches.txt WITHOUT running it, so re-run them here (idempotent). Schema before data.
	"tatva_connect.schema_setup.apply_schema",
	# Lock the stock-open doctype permission matrix on shared/core doctypes (Layer-1 VAPT fix); structural + idempotent, same reason as schema_setup above.
	"tatva_connect.access.lockdown.apply",
	# Fixture Property Setters land in sync_fixtures, AFTER sync_all's updatedb — so a widened fieldtype needs its column rebuilt here or it never follows the meta. A patch cannot do this: patches run before fixtures.
	"tatva_connect.lead_sync.schema.reconcile_fieldtypes",
	# The seven lead sections a field key is routed by; before any catalog row that Links to one.
	"tatva_connect.partner_api.section_seed.ensure_rows",
	# The three catalog rows the Facebook fold stamps by field_key; a key with no row has no declared home.
	"tatva_connect.lead_sync.catalog_seed.ensure_rows",
	# The token-expiry alert as a native Notification, seeded DISABLED — no job is written; Frappe owns the Days Before scheduler.
	"tatva_connect.lead_sync.notification_seed.ensure_notification",
	# Per-grain INTERNAL visibility contracts (is_internal=1) moved OUT of after_migrate to the seed tail (db-seeds/2026-07-24-internal-contracts.bench-console.py): they derive from the taxonomy MASTERS + the lead-field CATALOG, both MANUAL seeds that land AFTER migrate, so on a fresh Day-0 site after_migrate ran with no masters and (via the _masters_exist guard) built NOTHING silently — every rep saw zero grain fields. Built at the tail of apply-seeds now, where masters + catalog exist. ensure_internal_contracts stays additive + idempotent.
	# Master-data seeds run BEFORE the drift asserts below so a registry-drift throw never skips them; depend only on schema + fixtures (already applied); idempotent.
	"tatva_connect.seeds.seed_master_data",
	# Dashboard cards + the one seeded role layout; after master data because a layout Links to a Role, and after fixtures because a card names custom_* columns that land in sync_fixtures.
	"tatva_connect.dashboard.seed.ensure_rows",
	# Automation control plane: seed the catalog rows, then assert no doc_event/scheduler path drifts out of the registry (catalog after schema, drift after rows exist).
	"tatva_connect.automation.seed.sync_catalog",
	# Sync toggle-owned infrastructure (log-clear registration, scheduled-job stopped flag) to each row's state.
	"tatva_connect.automation.seed.reconcile_activations",
	# Same idea for the FTS index: it is a FILE, not a table, so no patch can reshape it and CREATE VIRTUAL TABLE IF NOT EXISTS silently keeps the old columns — an index whose stamped schema is stale is dropped and rebuilt here, every migrate, for ever.
	"tatva_connect.search.activation.reconcile_index_schema",
	# Stop the third-party jobs this site can never use (docs/investigations/scheduled-jobs-audit.md); after the toggles settle, and disjoint from them by test_scheduler_denylist.
	"tatva_connect.scheduler_denylist.apply",
	"tatva_connect.automation.drift.assert_registered",
	# Every notification grain must point at a real automation row (the ONE global gate); a drifting catalog fails the migrate.
	"tatva_connect.notifications.drift.assert_registered",
	# M2 guard: a file's bytes are in Azure, so a new call site that reads one off the local disk fails the migrate — ask FileOverride, never the disk.
	"tatva_connect.storage.drift.assert_no_disk_reads",
	# Layer-4 guard: fail the migrate if a locked doctype drifts open to All/Guest.
	"tatva_connect.access.lockdown.assert_locked",
	# The ledger now feeds apply(); LOCKED_MATRIX is the frozen reference it was seeded from. Retires when the first app is armed in ENFORCED_APPS.
	"tatva_connect.access.lockdown.assert_ledger_parity",
	# A site that ARMED the workflow engine without registering its `workflow` worker lane writes timer alarms into a queue nothing services — every run parks, every alarm is set, and none of them ever fires. Silent everywhere except here.
	"tatva_connect.workflow_engine.wakeups.assert_lane_registered",
	"tatva_connect.form_scripts_seed.seed",
	"tatva_connect.client_scripts_seed.seed",
	"tatva_connect.api.email.ensure_draft_folder",
	# Desk tiles — the four things the desktop_icon/ fixtures cannot express: gate the "Learning"/"Wiki"
	# workspaces' roles, hide the auto-generated orphan tile, hide wiki's role-gated App tile, and hold
	# every grouping tile at icon_type App (a Folder draws nothing). Runs after sync_all/sync_fixtures,
	# so it corrects rows the fixture import declined to touch. Idempotent; re-asserted every migrate.
	"tatva_connect.desktop_icon_reconcile.reconcile",
	# Drop wiki's install-time demo space ("Wiki" at /docs): it sorts above the handbook in wiki's own
	# space switcher, and /docs is the API reference site. A patch cannot do this — install_app marks
	# patches completed WITHOUT running them, so a fresh site would keep it for ever. Idempotent; the
	# space is left alone if anyone has written a real page under it.
	"tatva_connect.wiki_reconcile.reconcile",
	# Phase 3 of the task-sections plan: every answer a task ALREADY carries gets the home field_target names. Here and not only in its patch because the answers land in fixture Table fields routed by the section seed above — both AFTER post-model-sync patches, so on the upgrade that carries the whole chain in one migrate the patch runs before its own prerequisites and is logged applied. Idempotent; writes only what is missing.
	"tatva_connect.activity.backfill.ensure_section_rows",
	# Finish the custom_is_planned stamp. Here and not only in its patch because the column ships in custom_field.json, so the post-model-sync patch ALWAYS runs before sync_fixtures lands it, no-ops and is logged applied — dead. One-shot: the due date is sound evidence only for rows written before the stamp, so the pass marks itself done and never judges a row twice.
	"tatva_connect.tasks.plan_origin.backfill_is_planned",
	# LAST, deliberately: apply_schema records a failed structural step instead of throwing on the spot, because it is entry two of twenty-four and a throw there skipped every seed, drift assert and the lockdown behind it. The run still fails — after everything else has had its chance to land.
	"tatva_connect.schema_setup.assert_schema_applied",
]

# The SAME chain on the install path: `after_sync` fires at installer.py:343, after sync_fixtures (:339) — where after_migrate sits on the upgrade path. `after_install` (:332) was rejected: it runs BEFORE fixtures, and section_seed/reconcile_fieldtypes would skip on fields that do not exist yet. One list, two doors.
after_sync = after_migrate

# Schema-as-code: the custom_provider Select on WhatsApp Account ships as a fixture (the CRM WhatsApp Settings doctype ships as its own doctype JSON).
fixtures = [
	# Desk STRUCTURE (Workspace + Workspace Sidebar) is NOT fixtures — migrate's remove_orphan_entities() prunes any standard space with no backing FILE, so each ships as STANDARD FILES (model-sync auto-imports them). Only dashboard CONTENT below stays fixtures.
	# EDITING ONE: bump its `modified` in the JSON, or the edit does NOT ship. import_file.py:141 skips a standard file whose `modified` is <= the DB row's, so a Workspace/Workspace Sidebar edit migrates silently green and changes nothing. DocTypes are EXEMPT from that skip (they compare by migration_hash, import_file.py:130), which is why doctype JSONs need no bump.
	# Observability dashboard records — charts/cards aren't in IMPORTABLE_DOCTYPES (no module-folder sync), so they ship as name-scoped fixtures (the Dashboard Chart SOURCE is module-standard and syncs on migrate); name-filtered so export never vacuums other apps'.
	# "Automation Run Log by Outcome" is the automation engine's Run Log chart (Task 12, A.17 — the LAYOUT ships here, rows never seeded).
	# Workspace-P1: native grain lens (Run Log grain/outcome/trigger_doctype/rule) + Partner API leads-by-vertical
	# + Observability's Error Log group-by, embedded in the Automations/Partner API/Observability workspace content.
	{"dt": "Dashboard Chart", "filters": [["name", "in", [
		"API Traffic (Daily)", "API Errors (Daily)", "API Error Rate (Daily)",
		"API p95 Latency (Daily)", "API p95 Latency (Hourly)", "API Requests by Endpoint",
		"Automation Run Log by Outcome", "Automation Fires by Grain", "Automation Fires by Doctype",
		"Automation Top Rules", "Automation Fires (Daily)", "Automation Health by Grain",
		"Partner Leads by Vertical", "Errors by Reference Doctype",
	]]]},
	{"dt": "Number Card", "filters": [["name", "in", [
		"API Requests (24h)", "API Errors (24h)", "API Error Rate (24h)", "API p95 Latency (24h)",
		"Automation Fires Today", "Automation Enabled Rules", "Automation Failed Fires (7d)",
		"Partner API Requests (24h)", "Partner API Errors (24h)",
		"Automation Active Grains", "Automation Failure Rate (7d)",
	]]]},
	# Workspace-P2: the grain x log-source heatmap was a Custom HTML Block; retired. Health-by-grain
	# is now a native Dashboard Chart (chart_type=Custom, source "Automation Health by Grain").
	{
		"dt": "Custom Field",
		# Full parity (schema-as-code): ship EVERY custom field we add to these native doctypes so a fresh migrate reproduces the entire schema; every Custom Field here is ours; workflow_state is Frappe-managed (excluded).
		"filters": [
			["dt", "in", ["CRM Lead", "CRM Task", "CRM Program", "CRM Call Log", "CRM Telephony Agent", "WhatsApp Account", "File", "CRM Dashboard"]],
			["fieldname", "!=", "workflow_state"],
		],
	},
	# Shared doctypes (frappe_whatsapp / frappe-core / crm own them): ship ONLY our custom fields BY NAME —
	# never the dt-in vacuum — so export never sweeps the owner's fields. custom_provider_message_id backs
	# the WhatsApp dedup/composite index; grain_* drive grain-scoped assignment; custom_lsq_activity_id is
	# the LSQ idempotency key (notes; the CRM Task copy ships via the CRM Task dt-in filter above).
	{"dt": "Custom Field", "filters": [["name", "in", [
		"WhatsApp Message-custom_failed_reason",
		"WhatsApp Message-custom_provider_message_id",
		# An interactive reply's machine-readable identity — the two facts the upstream doctype has nowhere to put.
		"WhatsApp Message-custom_button_id",
		"WhatsApp Message-custom_button_title",
		# The run+node that sent this message — what makes a delivery receipt wake THAT run and no other.
		"WhatsApp Message-custom_workflow_correlation",
		# What an inbound button tap points back at. NOT custom_provider_message_id, which carries WATI's internal `id` and is the cross-path dedup key.
		"WhatsApp Message-custom_outbound_wamid",
		"Assignment Rule-grain_vertical",
		"Assignment Rule-grain_group",
		"Assignment Rule-grain_program",
		"FCRM Note-custom_lsq_activity_id",
		# The contract a Facebook form's leads are created against — the ONE place its grain and field set are declared.
		"Lead Sync Source-routing_section",
		"Lead Sync Source-api_mapping",
		# Stamped from Graph when the token is saved; a 60-day lapse otherwise stops the crawl in silence.
		"Lead Sync Source-token_expires_on",
		# The app that issued the source's token — a token belongs to exactly one, and only it can exchange it.
		"Lead Sync Source-facebook_app",
		# Provenance of the stored Page token, not ownership: a Business owns Pages and apps alike.
		"Facebook Page-facebook_app",
	]]]},
	# Field-property overrides on CRM data-model doctypes (option-less profile Select fields -> free-text, so form-written values store AND display).
	{"dt": "Property Setter", "filters": [["name", "in", [
		# No transactional doctype mints its name from an application counter. A naming_series name comes
		# from ONE row in tabSeries whose lock is held until commit, so concurrent creates deadlock on it:
		# 1 of 32 survived a 32-way burst, against 32 of 32 with a hash. autoincrement stays as it is — it
		# uses MariaDB's own sequence, which releases immediately and does not deadlock. Masters and
		# CRM Call Log (whose id is the telephony provider's) are untouched.
		# See patches/hash_name_transactional_doctypes.py.
		# A dashboard is a role's, arranged by an operator: upstream's personal-dashboard fields render nowhere and are hidden rather than left as a trap.
		"CRM Dashboard-layout-hidden",
		"CRM Dashboard-user-hidden",
		"CRM Dashboard-private-hidden",
		"CRM Lead-main-autoname",
		"CRM Lead-main-naming_rule",
		"CRM Deal-main-autoname",
		"CRM Deal-main-naming_rule",
		# P9: nivo_indication moved Plan -> Drug Program Profile; its free-text override follows the field (migration recreates here + drops the stale Plan ones).
		"CRM Drug Program Profile-nivo_indication-fieldtype",
		"CRM Drug Program Profile-nivo_indication-options",
		# Facebook question label/key are Data(140); live Goodflip forms carry 292-char qualification questions. The key is FB's slug of the label, so it is always the same length and widens with it — truncating it would silently stop the field_data match. See docs/plans/2026-07-16-facebook-lead-sync-remediation.md.
		"Facebook Lead Form Question-label-fieldtype",
		"Facebook Lead Form Question-key-fieldtype",
		# The page token is derived from a long-lived User token and never expires on its own; it is stored encrypted, not as plaintext Small Text.
		"Facebook Page-access_token-fieldtype",
		# "Unconfigured Form": the crawl's word for a source pointing at a form Facebook no longer reports.
		"Failed Lead Sync Log-type-options",
		# Upstream defaults enabled to 1; every automation surface here ships dormant and the operator turns it on.
		"Lead Sync Source-enabled-default",
		# Scoping fix: exclude the secondary (history) Link fields from User Permission matching so a scoped user is filtered by the CURRENT field only (else blank/different history HIDES valid in-scope leads).
		"CRM Lead-custom_previous_program-ignore_user_permissions",
		"CRM Lead-custom_origin_vertical-ignore_user_permissions",
		# Program is rep-editable within a line: drop its field-level lock (permlevel 1 -> 0); vertical + group stay permlevel 1 (only managers/integration move a lead between lines).
		"CRM Lead-custom_current_program-permlevel",
		# Field governance: clinical fields are API-owned -> read-only. Identity fields (name/mobile/gender/dob) are NOT: a person must type them on the create form to bring a lead into existence (the lead does not exist yet). Making them read_only enforced nothing server-side and only hid them from the empty create form. Post-creation role-based lock is a separate server-side lifecycle rule, not a field flag. See patches/drop_lead_identity_read_only.py.
		# custom_patient_id stays read-only: it is minted by the source-system API, never typed on create.
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
		# Provider Bearer tokens are long JWTs (>300 chars); raise the token field's form length cap (300 -> 1000) so an operator can paste a real token (column is already TEXT — form-validation only).
		"WhatsApp Account-token-length",
		# Declutter: hide frappe_whatsapp's Meta-handshake fields we never use, so the form shows just our custom_webhook_token (values hidden, not dropped).
		"WhatsApp Account-webhook_verify_token-hidden",
		"WhatsApp Account-app_id-hidden",
		"WhatsApp Account-business_id-hidden",
		"WhatsApp Account-phone_id-hidden",
		"WhatsApp Account-version-hidden",
		# crm's tray types are Mention/Task/Assignment/WhatsApp; a missed call and a stage move are neither, so the Select is EXTENDED (never rewritten) and the SPA renders them with the default avatar.
		"CRM Notification-type-options",
		# insert_after moves a custom field, never a standard one — only field_order lifts upstream's url/token out of our ingress section into their own credentials section.
		"WhatsApp Account-main-field_order",
		"WhatsApp Account-url-label",
		"WhatsApp Account-url-description",
		"WhatsApp Account-token-label",
		"WhatsApp Account-token-description",
		# Provider + url + token is the minimum that can send; gated on the provider so an account with no adapter is never forced to carry another provider's fields.
		"WhatsApp Account-url-mandatory_depends_on",
		"WhatsApp Account-token-mandatory_depends_on",
		# Declutter WhatsApp Notification: hide Meta media/header/button/print fields our text-template path doesn't use, so the form shows just template + variable mapping + account.
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
# A bundle, not a raw /assets path: esbuild hashes the filename, so a deploy can never leave a browser on a cached copy.
app_include_js = "tatva_connect.bundle.js"

# The rendered-page door to the app's boot bag: frappe's own context hook, so no line of crm/www/crm.py moves.
update_website_context = "tatva_connect.api.boot.website_context"

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
# Fresh-install seeding runs on `after_sync`, declared beside `after_migrate` above — NOT on `after_install`, which fires BEFORE sync_fixtures and would run the chain against fields that do not exist yet. This comment said the opposite for months: `install-app` never fires `after_migrate` (migrate.py:202 is its only caller), so a fresh site ran NONE of it.
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

# CRM Call Media holds a call's artifact state and points at the File holding its audio. That pointer must never be able to REFUSE a delete: the blob's life is the call's life (M1), so deleting a call has to reach `File.on_trash` and reclaim the bytes, and a Link check would leave patient audio in the container for ever. Frappe's own hook for exactly this, and the same reason Communication and ToDo are on core's list.
# A derived execution record must not PIN the record it is about: the journey/event Dynamic Links to the subject made frappe refuse to delete any lead that had ever entered a workflow. The journeys are stopped on the lead's own on_trash first, and the event inbox is aged out by its reaper.
ignore_links_on_delete = ["CRM Call Media", "CRM Workflow Journey", "CRM Workflow Signal"]

# Request Events
# ----------------
# Observability: stamp a monotonic start on every request; the after_request logger reads it to compute latency for watched endpoints.
before_request = [
	"tatva_connect.observability.capture.stamp_start",
	# Stricter per-IP/per-phone rate limit on the enrolment web-form submit (scoped + gated inside).
	"tatva_connect.intake.guards.throttle_intake",
	# frappe_whatsapp rebuilds its notification map on each of its ELEVEN wildcard doc_events; memoise it for the request.
	"tatva_connect.whatsapp.notification_map.install",
	# A SCORM tree is a cache of its File, not storage: rebuild it from the blob when the disk no longer has it.
	"tatva_connect.storage.scorm_rehydrate.install",
]
# Rewrite framework-layer errors on partner-API paths into the unified error contract, then log one raw row per partner-API/webhook hit (runs last); both no-op for other endpoints.
after_request = [
	"tatva_connect.api._base.normalise_partner_response",
	"tatva_connect.observability.capture.log_request",
	# M2: delete the temp copies get_full_path() hydrated for this request — the other half of "cached per request".
	"tatva_connect.storage.file_override.discard_hydrated",
]

# Job Events
# ----------
# A worker saves documents too, so it pays the same per-event map rebuild a request does.
before_job = [
	"tatva_connect.whatsapp.notification_map.install",
]

# M2: a worker hydrates too (offload, imports, exports) — the same cleanup, or the bytes outlive the job.
after_job = [
	"tatva_connect.storage.file_override.discard_hydrated",
]

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

# NO default_log_clearing_doctypes hook: LogSettings.validate() re-appends every hooked doctype (add_default_logtypes), so the hook would defeat the activators' deregistration and run retention while the toggle is off. capture.apply_logging and file_screening.apply_scan_logging own this, tied to the operator toggle.

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

