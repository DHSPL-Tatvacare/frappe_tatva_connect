from tatva_connect.whatsapp import roles as whatsapp_roles

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
	# Facebook discovery/crawl through our Graph layer: Meta's reason surfaces, no silent empty, no token in logs.
	"Lead Sync Source": "tatva_connect.lead_sync.source.TatvaLeadSyncSource",
	# A question maps to a catalog field_key, checked against the form's contract; upstream compares bare fieldnames and throws on every edit.
	"Facebook Lead Form": "tatva_connect.lead_sync.form.TatvaFacebookLeadForm",
	# Webhook ingress: derive the indexed token digest and refuse a config that would reject every
	# call. Auth is infrastructure, never a toggleable automation, so it is bound here rather than
	# in doc_events. CRM Telephony Account gets the same two calls from its own controller.
	"WhatsApp Account": "tatva_connect.whatsapp.account.ChannelWhatsAppAccount",
	# Mask secrets on every Error Log row, whichever app wrote it: frappe's own make_request logs the
	# failing URL before our handler runs. Infrastructure, never a toggleable automation, hence bound here.
	"Error Log": "tatva_connect.observability.error_log.MaskedErrorLog",
}

# Rewire frappe_whatsapp's "Sync templates" endpoint to pull from the account's provider (read-only mirror), not Meta — for the desk button and any caller.
override_whitelisted_methods = {
	"frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.fetch": "tatva_connect.whatsapp.templates_sync.sync_templates",
	# Acefone rides crm's NATIVE call UI (no fork): phone icon -> Acefone bridge call, call-log fetch gains a playable recording path.
	"crm.integrations.exotel.handler.make_a_call": "tatva_connect.telephony.bridge.make_a_call",
	"crm.fcrm.doctype.crm_call_log.crm_call_log.get_call_log": "tatva_connect.telephony.bridge.get_call_log",
	# Mirror LSQ: surface Task created/closed in the Lead/Deal activity timeline (native omits it); derived on read, nothing stored.
	"crm.api.activities.get_activities": "tatva_connect.api.activities.get_activities",
	# Attach the standard _link_titles map so list/Kanban cells show a Link's clean title (its
	# doctype title_field) instead of the composite :: PK. Generic; delegates to native get_data.
	"crm.api.doc.get_data": "tatva_connect.api.list_link_titles.get_data",
	# The CRM Task list lenses resolve through CRM Task.default_list_data() so no slot or operational column reaches a rep picker; every other doctype delegates to native untouched.
	"crm.api.doc.get_filterable_fields": "tatva_connect.api.task_lenses.get_filterable_fields",
	"crm.api.doc.get_group_by_fields": "tatva_connect.api.task_lenses.get_group_by_fields",
	"crm.api.doc.sort_options": "tatva_connect.api.task_lenses.sort_options",
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
	# VAPT hardening — LMS (internal training, Mode 2): allow_guest + engine-bypass catalog reads; the
	# wrapper NARROWS a non-privileged caller to published rows (courses/batches) and strips the
	# creator email from job details. Can't be locked via DocPerm (methods bypass the engine).
	"lms.lms.utils.get_courses": "tatva_connect.access.native_guards.get_courses",
	"lms.lms.utils.get_batches": "tatva_connect.access.native_guards.get_batches",
	"lms.lms.api.get_job_details": "tatva_connect.access.native_guards.get_job_details",
	# Upstream LMS race (2.55.0, unfixed on develop): CourseOverview calls this with no course, so a
	# student sees "Course Content coming soon!" on every course. Shim recovers it from the Referer.
	"lms.lms.utils.get_course_outline": "tatva_connect.learning.outline.get_course_outline",
	# VAPT Jul — quiz assessment integrity: submit_quiz gets an atomic single-attempt guard (N2 race) +
	# a best-effort server-side timer (N6); get_quiz_with_questions stamps the open time the timer reads.
	"lms.lms.doctype.lms_quiz.lms_quiz.submit_quiz": "tatva_connect.access.native_guards.submit_quiz",
	"lms.lms.utils.get_quiz_with_questions": "tatva_connect.access.native_guards.get_quiz_with_questions",
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
	"CRM Workflow Run": "tatva_connect.workflow_engine.permissions.get_run_permission_query_conditions",
	"CRM Workflow Event": "tatva_connect.workflow_engine.permissions.get_event_permission_query_conditions",
	"CRM Workflow Step Log": "tatva_connect.workflow_engine.permissions.get_step_log_permission_query_conditions",
}
has_permission = {
	"CRM Task": "tatva_connect.tasks.permissions.has_task_permission",
	"CRM Call Log": "tatva_connect.telephony.permissions.has_call_log_permission",
	"CRM Workflow Run": "tatva_connect.workflow_engine.permissions.has_run_permission",
	"CRM Workflow Event": "tatva_connect.workflow_engine.permissions.has_event_permission",
	"CRM Workflow Step Log": "tatva_connect.workflow_engine.permissions.has_step_log_permission",
	"FCRM Note": "tatva_connect.notes.permissions.has_note_permission",
	"WhatsApp Message": "tatva_connect.whatsapp.permissions.has_whatsapp_message_permission",
}

# Global spotlight search — a native Frappe FTS5 search class. List-valued hook: this ADDS our class
# alongside helpdesk's and wiki's, each writing its own index db. Dormant until CRM Search Settings is on.
sqlite_search = ["tatva_connect.search.index.CRMLeadSearch"]

# Event-driven automations: each side-effect lives in its feature module; providers persist only their own records, every side-effect hangs off here.
doc_events = {
	# A finished lead_import job stamps its outcome back onto the CRM Lead Import it came from.
	"CRM Bulk Job": {"on_update": "tatva_connect.lead_import.api.follow_job_status"},
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
		],
		"on_update": [
			# tell the rep the lead is assigned to that its stage moved (fires only on the save that moved it)
			"tatva_connect.notifications.events.on_lead_stage_changed",
			# the spotlight index denormalises the lead's owner into a permission column; restamp it + its child rows
			"tatva_connect.search.index.reindex_on_lead_owner_change",
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
		],
		# Push: ping the assignee's devices when a task lands on them (gated, enqueued).
		"after_insert": [
			"tatva_connect.notifications.events.on_task_created",
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
	# LMS resolves an embed once, at paste time in the author's browser, and stores the iframe src in the block — so a Microsoft share link is claimable server-side, with no fork of the LMS frontend.
	"Course Lesson": {
		"before_save": "tatva_connect.learning.embeds.rewrite_office_links",
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
	# Per-form intake sinks are runtime custom DocTypes with no code hook — a single wildcard after_insert processes them; early-returns cheaply (cached set test) for every non-intake doctype.
	# Automation engine (Task 4): the unified (on_doctype, event) router rides the SAME wildcard - no per-doctype code push. A doctype is "live" for automation only because an enabled rule names it (router.live_doctypes, self-healing cache); every handler early-returns cheaply otherwise.
	# Automation engine (Task 5): the GUARD lane rides validate, synchronous, BEFORE the save commits -
	# a matched rule's guard actions (e.g. Require Fields) can frappe.throw and block the save.
	# Automation engine (Task 10): Deleted rides on_trash - the row still exists there (before removal),
	# so router.on_deleted captures subject + context synchronously; the effect lane still runs
	# after-commit like Created/Updated (router.py's on_deleted docstring has the full nuance).
	"*": {
		# Workflow engine: the Flow GUARD lane — a Require Location / Require Fields Flow enforces at
		# save time and can frappe.throw to block; dormant + in_workflow-guarded, cheap early-return.
		"validate": [
			"tatva_connect.workflow_engine.triggers.run_guards",
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
		"after_insert": "tatva_connect.storage.file_events.after_insert",
		"on_trash": "tatva_connect.storage.file_events.on_trash",
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
		# Daily: sweep abandoned email-draft staging files.
		"30 2 * * *": ["tatva_connect.api.email.purge_draft_attachments"],
		# Daily: trim logs/monitor.json.log — the one log frappe appends to without rotating (1 GB or 30 days, whichever first).
		"30 3 * * *": ["tatva_connect.observability.monitor_log.sweep"],
		# Daily: drop expired partner-API idempotency records.
		"0 4 * * *": ["tatva_connect.api._base.purge_idempotency_keys"],
		# Hourly: fail any async bulk job stranded InProgress past its worker timeout (worker died).
		"45 * * * *": ["tatva_connect.api.partner_bulk_worker.reap_stranded_jobs"],
		# Daily: purge finished async bulk jobs + results + payload past the retention window.
		"15 4 * * *": ["tatva_connect.api.partner_bulk_job.purge_expired_jobs"],
		# Every 15 min: wake due-timer Flow Instances + reconcile lost wakeups (F5).
		"*/15 * * * *": [
			"tatva_connect.workflow_engine.wakeups.sweep",
		],
		# Every 5 min: warn about a task falling due, and tell a rep about one already overdue (the operator's lead time goes as low as 5 min; both switches are read per pass).
		"*/5 * * * *": ["tatva_connect.notifications.events.sweep_task_due"],
		# Nightly: re-read Facebook Pages and lead forms, so a published or reworded question is seen without a button press.
		"0 1 * * *": ["tatva_connect.lead_sync.discovery.refresh_all_sources"],
	},
}

# Ship the CRM Form Scripts from their .js source files on every migrate — keeps them version-controlled and in sync.
after_migrate = [
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
	# The three activity sections an activity field's answer is routed by; after fixtures, because each names a Table field on CRM Task that lands there.
	"tatva_connect.taxonomy.task_section_seed.ensure_rows",
	# The token-expiry alert as a native Notification, seeded DISABLED — no job is written; Frappe owns the Days Before scheduler.
	"tatva_connect.lead_sync.notification_seed.ensure_notification",
	# Per-grain INTERNAL visibility contracts (is_internal=1) moved OUT of after_migrate to the seed tail (db-seeds/2026-07-24-internal-contracts.bench-console.py): they derive from the taxonomy MASTERS + the lead-field CATALOG, both MANUAL seeds that land AFTER migrate, so on a fresh Day-0 site after_migrate ran with no masters and (via the _masters_exist guard) built NOTHING silently — every rep saw zero grain fields. Built at the tail of apply-seeds now, where masters + catalog exist. ensure_internal_contracts stays additive + idempotent.
	# Master-data seeds run BEFORE the drift asserts below so a registry-drift throw never skips them; depend only on schema + fixtures (already applied); idempotent.
	"tatva_connect.seeds.seed_master_data",
	# Automation control plane: seed the catalog rows, then assert no doc_event/scheduler path drifts out of the registry (catalog after schema, drift after rows exist).
	"tatva_connect.automation.seed.sync_catalog",
	# Sync toggle-owned infrastructure (log-clear registration, scheduled-job stopped flag) to each row's state.
	"tatva_connect.automation.seed.reconcile_activations",
	# Stop the third-party jobs this site can never use (docs/investigations/scheduled-jobs-audit.md); after the toggles settle, and disjoint from them by test_scheduler_denylist.
	"tatva_connect.scheduler_denylist.apply",
	"tatva_connect.automation.drift.assert_registered",
	# Every notification grain must point at a real automation row (the ONE global gate); a drifting catalog fails the migrate.
	"tatva_connect.notifications.drift.assert_registered",
	# M2 guard: a file's bytes are in Azure, so a new call site that reads one off the local disk fails the migrate — ask FileOverride, never the disk.
	"tatva_connect.storage.drift.assert_no_disk_reads",
	# Layer-4 guard: fail the migrate if a locked doctype drifts open to All/Guest.
	"tatva_connect.access.lockdown.assert_locked",
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
]

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
	# Workspace-P2: the grain x log-source heatmap is a matrix — no native chart form covers
	# category x category, so it stays a Custom HTML Block that frappe.calls
	# automation.report.grain_log_matrix and draws a hand-rolled table (100% theme-token, zero
	# literal color). The health-by-grain widget is now a NATIVE Dashboard Chart (chart_type=Custom,
	# source "Automation Health by Grain" above) — retired from here; embedded via the Workspace
	# `custom_blocks` table + `content` JSON below, same name-scoped-fixture posture as Dashboard
	# Chart/Number Card above.
	{"dt": "Custom HTML Block", "filters": [["name", "in", [
		"Grain x Log Source Heatmap",
	]]]},
	{
		"dt": "Custom Field",
		# Full parity (schema-as-code): ship EVERY custom field we add to these native doctypes so a fresh migrate reproduces the entire schema; every Custom Field here is ours; workflow_state is Frappe-managed (excluded).
		"filters": [
			["dt", "in", ["CRM Lead", "CRM Task", "CRM Program", "CRM Call Log", "CRM Telephony Agent", "WhatsApp Account", "File"]],
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
	]]]},
	# Field-property overrides on CRM data-model doctypes (option-less profile Select fields -> free-text, so form-written values store AND display).
	{"dt": "Property Setter", "filters": [["name", "in", [
		# No transactional doctype mints its name from an application counter. A naming_series name comes
		# from ONE row in tabSeries whose lock is held until commit, so concurrent creates deadlock on it:
		# 1 of 32 survived a 32-way burst, against 32 of 32 with a hash. autoincrement stays as it is — it
		# uses MariaDB's own sequence, which releases immediately and does not deadlock. Masters and
		# CRM Call Log (whose id is the telephony provider's) are untouched.
		# See patches/hash_name_transactional_doctypes.py.
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
	# M2: delete the temp copies get_full_path() hydrated for this request — the other half of "cached per request".
	"tatva_connect.storage.file_override.discard_hydrated",
]

# Job Events
# ----------
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

