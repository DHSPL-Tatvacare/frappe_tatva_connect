"""The ONE catalog — every automation in `tatva_connect` is one row here.

Every automation is an operator toggle, gated via `is_enabled(key)`: ON -> the
tatva_connect hook rides and enforces; OFF -> it's dormant and native frappe/crm
behaviour stands. No locked/always-on class — the operator owns every `enabled`.

`fires_on`: Doc Event · Schedule · Provider call · Permission.
`Area` is derived — segment 1 of the composite key (`key.split("::")[0]`).
`purpose`: one paragraph in the same shape for every row — what happens while the row
is on, then what stands while it is off — followed by one `Example:` line naming the
moment a user meets it. Third person, no "you". Seeded into `description` on migrate.
`backs` = the real dotted paths the row covers; the drift check (drift.py) matches
hooks.py against these strings, so a new doc_event/scheduler path with no registry
row fails `bench migrate`.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Auto:
	key: str
	fires_on: str
	trigger_detail: str = ""
	purpose: str = ""
	backs: list = field(default_factory=list)
	requires: str = ""
	activator: str = ""


AUTOMATIONS = [
	Auto(
		key="WhatsApp::WATI::messaging",
		fires_on="Provider call",
		trigger_detail="whatsapp/api gate · WhatsApp Message · before_save",
		purpose=(
			"WhatsApp is opened up on the lead: the templates and messages sent out of the CRM, and "
			"the patient replies that come back, both ride the WATI account routed to that lead's "
			"grain. Off, the WhatsApp tab and its send actions are inert — nothing leaves and "
			"nothing lands.\n"
			"Example: an approved template is sent to a lead, and the patient's reply arrives back "
			"on that same lead's WhatsApp tab."
		),
		backs=["tatva_connect.whatsapp.webhook.pin_inbound_reference"],
	),
	Auto(
		key="WhatsApp::WATI::templates",
		fires_on="Schedule",
		trigger_detail="every 6h",
		purpose=(
			"The approved-template list is refreshed from WATI every six hours, so the picker a rep "
			"sends from is always the current one and nobody syncs it by hand. Off, the list is "
			"frozen at whatever the last pull left behind.\n"
			"Example: a reminder template approved in WATI by marketing appears in the Send Template "
			"picker within six hours."
		),
		backs=["tatva_connect.whatsapp.templates_sync.scheduled_sync_all"],
		requires="WhatsApp::WATI::messaging",
	),
	Auto(
		key="WhatsApp::WATI::backfill",
		fires_on="Schedule",
		trigger_detail="operator-armed · getMessages history pull",
		purpose=(
			"A lead's WhatsApp tab is topped up from WATI's message history: the full two-way thread "
			"is pulled, including replies typed straight into the WATI portal, and any message the "
			"live webhook missed is inserted, de-duplicated by WATI message id. The row is dormant "
			"and unscheduled by default; a cron is armed by the operator when a gap needs filling.\n"
			"Example: messages an agent answered inside the WATI portal during a webhook outage are "
			"brought onto the lead's WhatsApp tab."
		),
		backs=["tatva_connect.whatsapp.backfill.scheduled_backfill"],
		requires="WhatsApp::WATI::messaging",
	),
	Auto(
		key="Telephony::Acefone::calls",
		fires_on="Provider call",
		trigger_detail="telephony/api gate",
		purpose=(
			"Click-to-call and call logging are enabled on the Acefone line routed to the lead's "
			"grain. Off, a lead's number cannot be dialled from the CRM and no call is written back "
			"to it.\n"
			"Example: a lead's number is clicked, Acefone bridges the call to the rep's phone, and "
			"the call is pulled into that lead's call log."
		),
		backs=[],
	),
	Auto(
		key="Telephony::Acefone::reconcile",
		fires_on="Schedule",
		trigger_detail="operator-armed · call records pull",
		purpose=(
			"A lead's call log is topped up from Acefone's call records: the calls on the lead's "
			"routed line are pulled and any the live webhook missed are written in, de-duplicated by "
			"the provider's call id. Calls a rep logged by hand are never touched. The row is "
			"dormant and unscheduled by default; a cron is armed by the operator when a gap needs "
			"filling.\n"
			"Example: an afternoon of calls lost to a webhook outage is brought back onto the lead's "
			"call log."
		),
		backs=["tatva_connect.telephony.reconcile.scheduled_reconcile"],
		requires="Telephony::Acefone::calls",
	),
	Auto(
		key="Storage::Azure::offload",
		fires_on="Doc Event",
		trigger_detail="File · after_insert + on_trash",
		purpose=(
			"Every uploaded attachment is moved off the app server into Azure Blob storage, and its "
			"blob is removed when the file is deleted, which keeps the CRM disk lean. Off, files "
			"stay on the server exactly as frappe stores them.\n"
			"Example: a prescription photo attached to a lead is held in Azure and served back "
			"through a proxy link rather than kept on the server."
		),
		# link_attach_fields is part of the offload's contract, not a switch of its own: an offloaded URL is
		# remote, so core's linker declines it and the file<->record bond is ours to make.
		backs=[
			"tatva_connect.storage.file_events.after_insert",
			"tatva_connect.storage.file_events.on_trash",
			"tatva_connect.storage.file_events.link_attach_fields",
		],
	),
	Auto(
		key="Location::Google::capture",
		fires_on="Provider call",
		trigger_detail="location/api gate",
		purpose=(
			"On a visit type marked location-tracked, the rep's GPS is captured at completion and "
			"checked against the clinic's pinned location, and the result is recorded as a "
			"tamper-proof visit trail. Off, a visit is closed with no location captured and no check "
			"made.\n"
			"Example: a doctor visit is marked done, the rep's coordinates are captured, confirmed "
			"to be within the allowed radius of the clinic, and logged as a Visit Audit entry."
		),
		backs=[],
	),
	Auto(
		key="Location::NearMe::directory",
		fires_on="Provider call",
		trigger_detail="near_me/api gate",
		purpose=(
			"The Near Me page is powered: every doctor lead with a pinned clinic is drawn on a "
			"cross-business-line map and list, so a rep on the road can find and call the doctors "
			"around them. It opens only for a user holding the Field Map User role, and only while "
			"this row is on. Off, the page is not reachable at all.\n"
			"Example: doctors within 15 km of the phone's location are shown on a map, and a card is "
			"tapped to call one or get directions."
		),
		backs=[],
	),
	Auto(
		key="Task::Assignment::followup",
		fires_on="Doc Event",
		trigger_detail="ToDo · after_insert",
		purpose=(
			"A 'Call Lead' follow-up task is raised the moment a lead is assigned, so an assigned "
			"lead never goes cold. Off, the assignment stands on its own and no task is created.\n"
			"Example: a lead is assigned to a rep, and a 'Call Lead' task due in 24 hours appears on "
			"their list."
		),
		# The WhatsApp inbound reply-task was retired here (folded into a user-built rule on the new
		# WhatsApp Message automation subject), so this toggle now backs only the assignment follow-up.
		backs=[
			"tatva_connect.tasks.tasks.on_lead_assignment",
		],
	),
	Auto(
		key="Task::Review::mirror",
		fires_on="Doc Event",
		trigger_detail="CRM Task · on_update",
		purpose=(
			"A Document Review task's verdict is copied onto the document it reviewed, so the file "
			"itself carries an Approved or Rejected badge on the Attachments tab. Off, the verdict "
			"lives only on the review task and the file shows nothing.\n"
			"Example: a patient's uploaded prescription is approved on its review task, and the file "
			"on the lead's Attachments tab shows an Approved badge."
		),
		backs=[
			"tatva_connect.tasks.review_mirror.mirror_review_outcome",
		],
	),
	Auto(
		key="Storage::File::draft-cleanup",
		fires_on="Schedule",
		trigger_detail="daily 02:30",
		purpose=(
			"Attachments stranded by sent or abandoned draft emails are cleared each night, so "
			"orphaned files do not pile up; a blob shared with the sent copy is left alive. Off, the "
			"orphans are kept indefinitely.\n"
			"Example: a file attached to an email draft that was then discarded is removed at 02:30."
		),
		backs=["tatva_connect.api.email.purge_draft_attachments"],
	),
	Auto(
		key="Partner::Idempotency::cleanup",
		fires_on="Schedule",
		trigger_detail="daily 04:00",
		purpose=(
			"Partner-API idempotency records — the opt-in write-dedup store — are dropped each night "
			"once past the retention window, so they do not accumulate. Off, the keys are kept "
			"indefinitely.\n"
			"Example: a partner's retry-safety keys from the previous day are cleared at 04:00."
		),
		backs=["tatva_connect.api._base.purge_idempotency_keys"],
	),
	Auto(
		key="Notify::Lead::assigned",
		fires_on="Doc Event",
		trigger_detail="ToDo · after_insert",
		purpose=(
			"The rep a lead is assigned to is told about it — in-app while they are at their desk, a "
			"mobile push when they are away — and each rep opts in per notification. Off, the "
			"assignment is silent and the lead is found only by looking.\n"
			"Example: a manager assigns a lead, and the rep receives a 'New lead assigned' "
			"notification."
		),
		backs=[
			"tatva_connect.notifications.events.on_lead_assigned",
		],
	),
	Auto(
		key="Notify::WhatsApp::received",
		fires_on="Doc Event",
		trigger_detail="WhatsApp Message · after_insert",
		purpose=(
			"The rep a lead is assigned to is told when the patient replies on WhatsApp — in-app "
			"while they are at their desk, a mobile push when they are away — and each rep opts in "
			"per notification. Off, the reply waits silently on the tab.\n"
			"Example: a patient answers a template message, and the rep hears about it rather than "
			"finding it on their next visit to the tab."
		),
		backs=[
			"tatva_connect.notifications.events.on_whatsapp_received",
		],
	),
	Auto(
		key="Notify::Telephony::missed",
		fires_on="Doc Event",
		trigger_detail="CRM Call Log · on_update (status -> No Answer, inbound)",
		purpose=(
			"The rep a lead is assigned to is told when the patient's call went unanswered. It is "
			"always pushed, since a missed call is only worth knowing about before the patient gives "
			"up; each rep opts in per notification. Off, the missed call is found only by reading the "
			"call log.\n"
			"Example: a patient rings the program's number and nobody picks up, and the rep sees it "
			"at once."
		),
		backs=[
			"tatva_connect.notifications.events.on_call_missed",
		],
	),
	Auto(
		key="Notify::Task::due-soon",
		fires_on="Schedule",
		trigger_detail="every 5 min · tasks falling due inside the operator's lead time",
		purpose=(
			"A rep is warned that a task assigned to them is about to fall due. The lead time is the "
			"operator's, set in CRM Notification Settings, and each task is warned about once per due "
			"date, so a rescheduled task warns again. Off, nothing is sent ahead of the due time.\n"
			"Example: a call is due in an hour, and the rep is reminded while there is still time to "
			"make it."
		),
		requires="Notify::Task::assigned",
		backs=[
			"tatva_connect.notifications.events.sweep_task_due",
		],
	),
	Auto(
		key="Notify::Task::overdue",
		fires_on="Schedule",
		trigger_detail="every 5 min · tasks past their due date and not done",
		purpose=(
			"A rep is told when a task assigned to them has passed its due date and is still not "
			"done. Each task is told about once per due date, so a rescheduled task can tell again "
			"and no task nags on every sweep. Off, an overdue task passes unremarked.\n"
			"Example: yesterday's follow-up call was never made, and the rep is told rather than the "
			"lead going cold."
		),
		requires="Notify::Task::assigned",
		backs=[
			"tatva_connect.notifications.events.sweep_task_due",
		],
	),
	Auto(
		key="Notify::Lead::stage-changed",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · on_update (custom_substage changed)",
		purpose=(
			"The rep a lead is assigned to is told when its stage is moved, whoever moved it — a "
			"manager, an automation rule, or an integration — and each rep opts in per notification. "
			"Off, the lead changes hands quietly.\n"
			"Example: a manager moves a lead to Consent Pending, and the rep is told the ball is in "
			"their court."
		),
		backs=[
			"tatva_connect.notifications.events.on_lead_stage_changed",
		],
	),
	Auto(
		key="Notify::Task::assigned",
		fires_on="Doc Event",
		trigger_detail="CRM Task · after_insert",
		purpose=(
			"The rep a task is assigned to is told about it — in-app while they are at their desk, a "
			"mobile push when they are away — and each rep opts in per notification. Off, the "
			"assignment is silent and the task is found only by looking.\n"
			"Example: a manager assigns a task, and the rep receives a 'New task assigned' "
			"notification."
		),
		backs=[
			"tatva_connect.notifications.events.on_task_created",
		],
	),
	Auto(
		key="Lead::CRM Lead::dedup",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · before_validate + validate",
		purpose=(
			"Each lead's phone and routing fields are normalised, and a second lead for a patient "
			"already carried in the same product line is refused at save. Off, the duplicate is "
			"accepted and two records for one patient run in parallel.\n"
			"Example: a partner re-sends a patient who is already enrolled, the number is recognised, "
			"and the duplicate lead is not created."
		),
		# Phone + routing canonicalisation ride WITH dedup, not as separate switches:
		# the dedup anchor compares on the canonical form, so dedup silently misses
		# duplicates without them — one mechanism, one toggle.
		backs=[
			"tatva_connect.lead.leads.canonicalize_routing_fields",
			"tatva_connect.lead.leads.normalize_lead_phones",
			"tatva_connect.lead.leads.dedup_guard",
		],
	),
	Auto(
		key="Lead::CRM Lead::grain",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · before_validate",
		purpose=(
			"Each new lead is filed under its creator's entitled grain — product line, group, "
			"program — rather than the creator being asked to pick one: a single-grain user's grain "
			"is applied for them, a manager's choice is validated against their entitlement, and a "
			"grain that is missing or out of scope is rejected. Off, the grain is whatever is typed, "
			"unchecked.\n"
			"Example: a rep who works one program creates a lead without ever seeing a grain field; "
			"it is filed under their program, and one outside it cannot be filed at all."
		),
		backs=["tatva_connect.lead.leads.stamp_entitled_grain"],
	),
	Auto(
		key="Lead::CRM Lead::stage",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · validate",
		purpose=(
			"The program-specific lead lifecycle is enforced: only the stages that belong to that "
			"lead's program can be set, and the high-level stage is kept in step with them. Off, any "
			"stage can be set on any lead.\n"
			"Example: a stage that does not belong to the lead's program is chosen, and the save is "
			"blocked with a plain message."
		),
		backs=["tatva_connect.lead.leads.validate_stage"],
	),
	Auto(
		key="Task::CRM Task::guards",
		fires_on="Doc Event",
		trigger_detail="CRM Task · validate",
		purpose=(
			"Task completion is backstopped: the checklist is seeded from the template that matches "
			"the task, and the task cannot be marked Done until its checklist, its location and its "
			"activity log are all satisfied. Off, a task closes with none of them filled in.\n"
			"Example: a visit task is closed without the activity logged, and the save is blocked "
			"until it is."
		),
		backs=[
			"tatva_connect.tasks.tasks.seed_checklist",
			"tatva_connect.tasks.tasks.enforce_checklist",
			"tatva_connect.tasks.tasks.enforce_location",
			"tatva_connect.tasks.tasks.enforce_activity_logged",
		],
	),
	Auto(
		key="Task::CRM Task::visibility",
		fires_on="Permission",
		trigger_detail="CRM Task · permission_query_conditions + has_permission",
		purpose=(
			"CRM Tasks are scoped to the people who should see them: a task is visible to a rep only "
			"when it is theirs, assigned to them, or hangs off a Lead or Deal already visible to "
			"them. Stock crm scopes Leads and Deals but never Tasks, so off, every agent sees every "
			"task in the business.\n"
			"Example: the Tasks list shows a rep only the tasks on their own leads, not the whole "
			"team's."
		),
		# Gated by the accessor inside tasks/permissions.py (keyed on this row), NOT a
		# doc_event — so backs is empty, like Telephony/Location. The permission-hook
		# targets must stay OUT of the registry (drift walks only doc_events+scheduler;
		# perm/override targets are deliberately un-backed). See test_drift_ignores_overrides.
		backs=[],
	),
	Auto(
		key="Telephony::CRM Call Log::visibility",
		fires_on="Permission",
		trigger_detail="CRM Call Log · permission_query_conditions + has_permission",
		purpose=(
			"Call logs are scoped to the people who should see them: a call is visible to a rep only "
			"when it is theirs, or hangs off a Lead or Deal already visible to them. Stock crm scopes "
			"Leads and Deals but never call logs, so off, every agent sees every call in the "
			"business.\n"
			"Example: a lead's Calls tab shows a rep only the calls on their own leads, not the whole "
			"team's."
		),
		# Gated by the shared brain inside access/visibility.py (keyed on this row), NOT a
		# doc_event — so backs is empty, like Task::CRM Task::visibility. The permission-hook
		# targets stay OUT of the registry (drift walks only doc_events + scheduler).
		backs=[],
	),
	Auto(
		key="Note::FCRM Note::visibility",
		fires_on="Permission",
		trigger_detail="FCRM Note · permission_query_conditions + has_permission",
		purpose=(
			"Notes are scoped to the people who should see them: a note is visible to a rep only when "
			"it is theirs, or hangs off a Lead or Deal already visible to them. Stock crm scopes "
			"Leads and Deals but never notes, so off, every agent reads every note the team has "
			"written.\n"
			"Example: a lead's Notes tab shows a rep only the notes on their own leads, not the whole "
			"team's."
		),
		# Gated by the shared brain inside access/visibility.py (keyed on this row), NOT a
		# doc_event — so backs is empty, like Telephony::CRM Call Log::visibility. The
		# permission-hook targets stay OUT of the registry (drift walks only doc_events + scheduler).
		backs=[],
	),
	Auto(
		key="WhatsApp::WhatsApp Message::visibility",
		fires_on="Permission",
		trigger_detail="WhatsApp Message · permission_query_conditions + has_permission",
		purpose=(
			"WhatsApp messages are scoped to the people who should see them: a message is visible to "
			"a rep only when it is theirs, or hangs off a Lead or Deal already visible to them. Stock "
			"crm scopes Leads and Deals but never WhatsApp threads, so off, every agent reads every "
			"patient conversation.\n"
			"Example: a lead's WhatsApp tab shows a rep only the messages on their own leads, not the "
			"whole team's."
		),
		# Gated by the shared brain inside access/visibility.py (keyed on this row), NOT a
		# doc_event — so backs is empty, like Telephony::CRM Call Log::visibility. The
		# permission-hook targets stay OUT of the registry (drift walks only doc_events + scheduler).
		backs=[],
	),
	Auto(
		key="Task::Automation::sends",
		fires_on="Provider call",
		trigger_detail="automation/sends gate · Send WhatsApp / Send Email effect verbs",
		purpose=(
			"The send gate for the engine's Send WhatsApp and Send Email effects. Off, which is how it "
			"ships, a rule carrying a Send action still fires end to end and the Run Log records the "
			"intent, but no template and no email ever leaves the building. On, those same actions "
			"send for real, through the existing grain-routed WATI account and frappe's own mailer — "
			"never a second transport.\n"
			"Example: a 'Welcome' rule is built and tested with the gate off, the Run Log reading "
			"'suppressed: sends dormant', and the same rule starts sending the real message the day it "
			"is switched on at go-live."
		),
		backs=[],
		requires="Workflow::Engine::run",
	),
	Auto(
		key="Workflow::Engine::run",
		fires_on="Doc Event",
		trigger_detail='wildcard "*" · validate (guard lane) + after_insert (Created) + on_update (Updated) + on_trash (Deleted) — workflow entry',
		purpose=(
			"The workflow engine itself: a subject entering an enabled workflow whose grain matches "
			"starts one durable Instance that walks a graph of steps, branches, and waits, parking on "
			"timers and external signals for as long as the journey needs. One instance runs per subject, "
			"guarded by a database unique key so a subject can never start the same workflow twice, and "
			"an external event is delivered by dropping a row in a durable inbox that the matching wait "
			"consumes — so an early, duplicate, or out-of-order signal is handled by construction rather "
			"than lost. Off, which is how it ships, no instance starts and no signal is delivered.\n"
			"Example: a lead is created, a welcome step fires, the journey waits for a document upload, "
			"and the upload's signal resumes it weeks later exactly where it parked."
		),
		backs=[
			"tatva_connect.workflow_engine.triggers.run_guards",
			"tatva_connect.workflow_engine.triggers.on_created",
			"tatva_connect.workflow_engine.triggers.on_updated",
			"tatva_connect.workflow_engine.triggers.on_trash",
			"tatva_connect.workflow_engine.triggers.on_task_done",
		],
	),
	Auto(
		key="Workflow::Engine::sweep",
		fires_on="Schedule",
		trigger_detail="every 15 min · timer wake + reconciler re-drive",
		purpose=(
			"The scheduled heartbeat behind the workflow engine's waits: every 15 minutes it wakes each "
			"instance whose timer has elapsed and re-drives any instance whose awaited signal is already "
			"waiting in the inbox but whose wake was lost, so the durable state is always the source of "
			"truth and no lost job can strand a journey. It is gated on its own switch as well as the "
			"engine's, so the sweep can be paused without taking the engine down. Off, timers and "
			"buffered signals simply wait.\n"
			"Example: a journey parked on a two-day timer is resumed by the first sweep after the two "
			"days elapse, even if the original wake job never ran."
		),
		backs=[
			"tatva_connect.workflow_engine.wakeups.sweep",
		],
		requires="Workflow::Engine::run",
	),
	Auto(
		key="Storage::File::privacy",
		fires_on="Doc Event",
		trigger_detail="File · validate",
		purpose=(
			"Every attachment's privacy is decided at upload: a patient's file is held private, and "
			"only the doctypes the operator has whitelisted — logos and the like — are made public. "
			"Off, privacy is left to whatever frappe defaults to.\n"
			"Example: a patient's lab report is uploaded and marked private, so only an authorised "
			"user can open it."
		),
		backs=["tatva_connect.storage.file_events.apply_privacy_policy"],
	),
	Auto(
		key="Lead::CRM Lead::headline",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · validate",
		purpose=(
			"The newest clinical readings on a lead's lab records are surfaced onto the lead's summary "
			"fields, so the header shows the latest value rather than the first one. Off, the headline "
			"fields hold whatever was last written into them by hand.\n"
			"Example: a new lab report is added to a lead, and its HbA1c value appears on the lead's "
			"headline fields."
		),
		backs=["tatva_connect.lead.leads.sync_headline_metrics"],
	),
	Auto(
		key="Lead::Enrolment::intake",
		fires_on="Doc Event",
		trigger_detail="ANY per-form intake sink · after_insert (*)",
		purpose=(
			"A submitted enrolment or intake form is turned into a fully populated lead: every answer "
			"is mapped to its field and any master it needs is created on the way. Each intake form "
			"owns a runtime submission DocType, and a single wildcard after_insert routes all of them "
			"through the same lead-create brain, a cheap cached guard ignoring every doctype that is "
			"not an intake sink. Off, the submission is stored but no lead is built from it.\n"
			"Example: a patient submits the enrolment web form, and a lead appears with their details, "
			"their program and their attachments already filled in."
		),
		# route_submission = the wildcard brain for per-form sinks; bust_intake_doctype_cache = the
		# guard-set cache invalidator (a CRM Intake Form on_update/on_trash doc_event, hence backed
		# here too). Every intake form has its OWN per-form runtime sink — no shared legacy staging.
		backs=[
			"tatva_connect.intake.intake.route_submission",
			"tatva_connect.intake.intake.bust_intake_doctype_cache",
			# CRM Intake Form on_update -> scaffold/sync the per-form DocType + Web Form.
			"tatva_connect.intake.builder.sync_form",
		],
	),
	Auto(
		key="Intake::RateLimit::enforcement",
		fires_on="Provider call",
		trigger_detail="before_request · web-form accept · per-IP + per-phone counters",
		purpose=(
			"Enrolment-form submissions are capped per IP and per phone, above frappe's native ten a "
			"minute per IP, so a bot or a stuck client cannot flood the form; the caps themselves are "
			"tunable in CRM Intake Settings. Off, the native per-IP limit alone applies, which is "
			"today's behaviour.\n"
			"Example: a script POSTing the form in a loop is throttled with a plain 'try again later', "
			"while a patient submitting once is unaffected."
		),
		# before_request gate — drift walks only doc_events/scheduler, so backs is empty
		# (like the visibility/partner-rate-limit gates).
		backs=[],
	),
	Auto(
		key="Storage::File::screening",
		fires_on="Doc Event",
		trigger_detail="File · before_insert (intake) + partner file API",
		purpose=(
			"The master switch for the shared file screener: a file's bytes are sniffed against the "
			"type it claims to be, and the file is scanned by ClamAV, over and above frappe's native "
			"size, extension and unsafe-PDF checks. Only the channels the operator lists in CRM File "
			"Screening Settings → Active Channels (Intake, Partner API) are screened, so covering a "
			"new channel is a config edit rather than a code change. Off, the native checks stand "
			"alone. The ClamAV container is needed for the scan.\n"
			"Example: with Partner API listed as active, an .exe renamed to .pdf and sent to "
			"file_attach is refused, and an infected upload is blocked."
		),
		backs=["tatva_connect.intake.guards.guard_file"],
		activator="tatva_connect.storage.file_screening.apply_scan_logging",
	),
	Auto(
		key="Partner::Catalog::cache",
		fires_on="Doc Event",
		trigger_detail="CRM Lead API Field · on_update + on_trash",
		purpose=(
			"The partner-API field catalog is refreshed the moment its definition changes, so an "
			"external partner always fetches the current list of fields. Off, a partner is served the "
			"cached list until it expires of its own accord.\n"
			"Example: an admin adds a new API field, and the partner's very next call reflects it "
			"instead of returning a stale catalog."
		),
		backs=["tatva_connect.api.partner.clear_catalog_cache"],
	),
	Auto(
		key="Partner::RateLimit::enforcement",
		fires_on="Provider call",
		trigger_detail="api/_base · @_api + _run_bulk gate",
		purpose=(
			"The partner API is throttled, so that no one partner — and no flood of them together — "
			"can starve the shared web workers that also serve the CRM. Off, nothing is throttled and "
			"the API behaves exactly as it does today.\n"
			"Example: a partner script looping too fast is answered with a 429 and a Retry-After, and "
			"the CRM stays responsive for everyone else."
		),
		# A gate inside _base (@_api + _run_bulk), NOT a doc_event/scheduler — like
		# Telephony/Location/visibility, so backs is empty (drift walks only doc_events
		# + scheduler paths; this gate's target stays OUT of the registry).
		backs=[],
	),
	Auto(
		key="Partner::AsyncBulk::jobs",
		fires_on="Provider call",
		trigger_detail="api/partner_bulk_job · submit gate",
		purpose=(
			"The asynchronous bulk-job tier is opened up: a partner may submit a high-volume create job, "
			"which a dedicated background worker drains through the same create brain the sync endpoints "
			"use, reporting completion by webhook and a status endpoint. Off, a submit is refused and no "
			"job runs; the synchronous single and slim-bulk endpoints are unaffected either way.\n"
			"Example: a partner posts a 10,000-lead job, receives a job id at once, and polls it to "
			"JobComplete while the CRM stays responsive."
		),
		# The submit gate lives inside api/partner_bulk_job (not a doc_event), so the toggle has no
		# doc_event target; the tier's one doc_event is the always-on SSRF guard on a partner's
		# completion-webhook URL, covered here so the drift lock passes.
		backs=["tatva_connect.api.partner_bulk_job.guard_webhook_url"],
	),
	Auto(
		key="Partner::AsyncBulk::reaper",
		fires_on="Schedule",
		trigger_detail="hourly at :45",
		purpose=(
			"An async bulk job left in-progress past its worker timeout — because the worker was "
			"killed, redeployed or ran out of memory — is marked failed and its stored upload dropped, "
			"so it cannot sit half-done forever and the partner is told by the completion webhook. Off, "
			"a stranded job stays in-progress until it is cleared by hand.\n"
			"Example: the queue worker is restarted mid-drain, and within the hour the job is failed "
			"with its payload purged."
		),
		backs=["tatva_connect.api.partner_bulk_worker.reap_stranded_jobs"],
	),
	Auto(
		key="Partner::AsyncBulk::purge",
		fires_on="Schedule",
		trigger_detail="daily 04:15",
		purpose=(
			"Finished async bulk jobs — their per-record results and the uploaded payload — are dropped "
			"each day once past the retention window, so the tables and blob store do not grow without "
			"bound. Off, finished jobs and their uploads are kept indefinitely.\n"
			"Example: a job that completed eight days ago (retention seven) is removed at 04:15, results "
			"and file included."
		),
		backs=["tatva_connect.api.partner_bulk_job.purge_expired_jobs"],
	),
	Auto(
		key="Activity::Metrics::rollup",
		fires_on="Doc Event",
		trigger_detail="CRM Task · on_update/on_submit/on_cancel/on_trash",
		purpose=(
			"A lead's per-activity counts — demo visits, courtesy calls and the rest — are kept true "
			"by recomputing the affected count from the task table whenever an activity task changes. "
			"The recompute is absolute rather than incremental, so a count heals itself and cannot "
			"drift. Off, the counts stand as they were loaded, and loading activities fires no per-row "
			"work.\n"
			"Example: a 'Demo Visit' task is marked Done, and the lead's Demo Visit count settles at "
			"the true number of completed demo visits."
		),
		backs=["tatva_connect.tasks.metrics.refresh_for_lead"],
	),
	Auto(
		key="Observability::Requests::logging",
		fires_on="Provider call",
		trigger_detail="after_request · partner-API + inbound-webhook endpoints",
		purpose=(
			"Every partner-API call and inbound webhook is written to CRM API Request Log, which is "
			"what feeds the observability dashboard. Off, nothing is captured and the dashboard has "
			"nothing to show.\n"
			"Example: a partner's lead_create call is recorded with its endpoint, its status and its "
			"latency."
		),
		backs=["tatva_connect.observability.capture.log_request"],
		activator="tatva_connect.observability.capture.apply_logging",
	),
	Auto(
		key="Observability::Metrics::rollup",
		fires_on="Schedule",
		trigger_detail="every 6h",
		purpose=(
			"The raw request log is rolled up into compact metric tables every six hours, so "
			"dashboards and health checks are answered from a summary instead of the database being "
			"bloated by their queries. Off, the charts hold whatever the last rollup left behind.\n"
			"Example: the request-volume and latency charts on the operations dashboard refresh on "
			"this schedule."
		),
		backs=["tatva_connect.observability.rollup.run"],
		requires="Observability::Requests::logging",
		activator="tatva_connect.observability.rollup.apply_rollup",
	),
	Auto(
		key="Observability::Monitor::log-sweep",
		fires_on="Schedule",
		trigger_detail="daily 03:30",
		purpose=(
			"Trims the request monitor's log file each night, to whichever limit is reached first: 1 GB "
			"or 30 days. It is the one log frappe never rotates, because it is written with a plain "
			"append rather than through the rotating logger. Off, the file grows without bound on the "
			"app server's disk.\n"
			"Example: a night on which logs/monitor.json.log has passed 1 GB, it is compressed to an "
			"archive and emptied, and archives older than 30 days are deleted."
		),
		backs=["tatva_connect.observability.monitor_log.sweep"],
	),
]


def activator_for(key):
	for auto in AUTOMATIONS:
		if auto.key == key:
			return auto.activator
	return ""
