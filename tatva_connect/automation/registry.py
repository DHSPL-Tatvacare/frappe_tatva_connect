"""The ONE catalog — every automation in `tatva_connect` is one row here.

Every automation is an operator toggle, gated via `is_enabled(key)`: ON -> the
tatva_connect hook rides and enforces; OFF -> it's dormant and native frappe/crm
behaviour stands. No locked/always-on class — the operator owns every `enabled`.

`fires_on`: Doc Event · Schedule · Provider call.
`Area` is derived — segment 1 of the composite key (`key.split("::")[0]`).
`purpose`: the plain-English "what this does" + one UX example, shown read-only in
the form (seeded into `description` on every migrate — code-owned reference text).
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


AUTOMATIONS = [
	Auto(
		key="WhatsApp::WATI::messaging",
		fires_on="Provider call",
		trigger_detail="whatsapp/api gate · WhatsApp Message · before_save",
		purpose=(
			"Turns WhatsApp on for leads — outbound template/message sends and inbound patient "
			"replies both flow through the lead's routed WATI account.\n"
			"Example: a rep sends an approved template to a lead, and the patient's reply lands "
			"back on that same lead's WhatsApp tab."
		),
		backs=["tatva_connect.whatsapp.webhook.pin_inbound_reference"],
	),
	Auto(
		key="WhatsApp::WATI::templates",
		fires_on="Schedule",
		trigger_detail="every 6h",
		purpose=(
			"Keeps the approved-template list current by pulling from WATI every 6 hours, so reps "
			"always pick from the latest templates without anyone syncing by hand.\n"
			"Example: marketing approves a new reminder template in WATI; within 6 hours it shows "
			"up in the Send Template picker."
		),
		backs=["tatva_connect.whatsapp.templates_sync.scheduled_sync_all"],
		requires="WhatsApp::WATI::messaging",
	),
	Auto(
		key="Telephony::Acefone::calls",
		fires_on="Provider call",
		trigger_detail="telephony/api gate",
		purpose=(
			"Enables click-to-call and call logging through the lead's routed Acefone line.\n"
			"Example: a rep clicks a lead's number, Acefone bridges the call to their phone, and "
			"the call is pulled into the lead's call log."
		),
		backs=[],
	),
	Auto(
		key="Storage::Azure::offload",
		fires_on="Doc Event",
		trigger_detail="File · after_insert + on_trash",
		purpose=(
			"Moves every uploaded attachment off the app server into Azure Blob storage, and "
			"removes the blob when the file is deleted — keeping the CRM disk lean.\n"
			"Example: a rep attaches a prescription photo to a lead; the image is stored in Azure "
			"and served back through a proxy link, not kept on the server."
		),
		backs=[
			"tatva_connect.storage.file_events.after_insert",
			"tatva_connect.storage.file_events.on_trash",
		],
	),
	Auto(
		key="Location::Google::capture",
		fires_on="Provider call",
		trigger_detail="location/api gate",
		purpose=(
			"For location-tracked visit types, captures the rep's GPS at completion and checks it "
			"against the clinic's location, recording the result as a tamper-proof visit trail.\n"
			"Example: a rep marks a doctor visit done; the app captures their coordinates, confirms "
			"they're within the allowed radius of the clinic, and logs a Visit Audit entry."
		),
		backs=[],
	),
	Auto(
		key="Task::Assignment::followup",
		fires_on="Doc Event",
		trigger_detail="ToDo · after_insert · WhatsApp Message · after_insert",
		purpose=(
			"Raises the right follow-up task automatically so a lead never goes cold — a 'Call "
			"Lead' task when a lead is assigned, and a reply task when a patient messages in.\n"
			"Example: a lead is assigned to a rep; a 'Call Lead' task due in 24 hours appears on "
			"their list."
		),
		backs=[
			"tatva_connect.tasks.tasks.on_lead_assignment",
			"tatva_connect.whatsapp.inbound.on_inbound_message",
		],
	),
	Auto(
		key="Storage::File::draft-cleanup",
		fires_on="Schedule",
		trigger_detail="daily 02:30",
		purpose=(
			"Each night, clears attachments left behind by sent or abandoned draft emails so "
			"orphaned files don't pile up (a shared blob is kept alive for the sent copy).\n"
			"Example: a rep attaches a file to an email, discards the draft, and the orphaned file "
			"is removed at 02:30."
		),
		backs=["tatva_connect.api.email.purge_draft_attachments"],
	),
	Auto(
		key="Notify::Lead::assigned",
		fires_on="Doc Event",
		trigger_detail="ToDo · after_insert",
		purpose=(
			"Notifies a rep when a new lead is assigned to them — in-app while they're online, "
			"a mobile push when they're away. The rep opts in per notification.\n"
			"Example: a manager assigns a lead; the rep gets a 'New lead assigned' notification."
		),
		backs=[
			"tatva_connect.notifications.events.on_lead_assigned",
		],
	),
	Auto(
		key="Notify::Task::assigned",
		fires_on="Doc Event",
		trigger_detail="CRM Task · after_insert",
		purpose=(
			"Notifies a rep when a new task is assigned to them — in-app while they're online, "
			"a mobile push when they're away. The rep opts in per notification.\n"
			"Example: a manager assigns a task; the rep gets a 'New task assigned' notification."
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
			"Normalises each lead's phone and routing fields, then blocks a second lead being "
			"created for a patient already in the same product line.\n"
			"Example: a partner re-sends a patient who's already enrolled; the system recognises "
			"the number and stops a duplicate lead from being created."
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
		key="Lead::CRM Lead::stage",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · validate",
		purpose=(
			"Enforces the program-specific lead lifecycle — only stages valid for that lead's "
			"program can be set, and the high-level stage is kept in sync.\n"
			"Example: a rep tries to set a stage that doesn't belong to the lead's program; the "
			"save is blocked with a clear message."
		),
		backs=["tatva_connect.lead.leads.validate_stage"],
	),
	Auto(
		key="Task::CRM Task::guards",
		fires_on="Doc Event",
		trigger_detail="CRM Task · validate",
		purpose=(
			"Backstops task completion: fills the task's checklist from the right template and "
			"refuses to mark it Done until its checklist, location, and activity log are satisfied.\n"
			"Example: a rep tries to close a visit task without logging the activity; the save is "
			"blocked until they complete it."
		),
		backs=[
			"tatva_connect.tasks.tasks.seed_checklist",
			"tatva_connect.tasks.tasks.enforce_checklist",
			"tatva_connect.tasks.tasks.enforce_location",
			"tatva_connect.tasks.tasks.enforce_activity_logged",
		],
	),
	Auto(
		key="Task::Automation::rules",
		fires_on="Doc Event",
		trigger_detail="CRM Task · on_update",
		purpose=(
			"The automation engine: when an activity task is marked Done, runs every enabled "
			"rule whose grain matches the lead — checking its criteria, then firing its actions "
			"(create a task, set a field) in order.\n"
			"Example: an operator builds a rule for the Diabetes program so that completing a "
			"'First Consult' task whose outcome is 'Enrolled' creates a 'Welcome Call' task."
		),
		backs=["tatva_connect.automation.dispatcher.fire_rules"],
	),
	Auto(
		key="Task::Automation::run-log-sweep",
		fires_on="Schedule",
		trigger_detail="daily 03:00",
		purpose=(
			"Each night, prunes automation Run Log rows past the retention window so the audit "
			"trail stays useful without growing without bound.\n"
			"Example: run-log entries older than the retention period are removed at 03:00."
		),
		backs=["tatva_connect.automation.dispatcher.sweep_run_log"],
	),
	Auto(
		key="Storage::File::privacy",
		fires_on="Doc Event",
		trigger_detail="File · validate",
		purpose=(
			"Sets every attachment's privacy on upload — patient files stay private, and only "
			"operator-whitelisted doctypes (e.g. logos) are public.\n"
			"Example: a patient's lab report is uploaded and is automatically marked private, so "
			"only authorised users can open it."
		),
		backs=["tatva_connect.storage.file_events.apply_privacy_policy"],
	),
	Auto(
		key="Lead::CRM Lead::headline",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · validate",
		purpose=(
			"Surfaces the latest clinical headline values from the lead's lab records up onto the "
			"lead's summary fields, so the header always reflects the newest reading.\n"
			"Example: a new lab report is added to a lead; its latest HbA1c value appears on the "
			"lead's headline fields."
		),
		backs=["tatva_connect.lead.leads.sync_headline_metrics"],
	),
	Auto(
		key="Lead::Enrolment::intake",
		fires_on="Doc Event",
		trigger_detail="CRM Enrolment Submission · after_insert",
		purpose=(
			"Turns a submitted enrolment/intake form into a fully-populated lead, mapping each "
			"answer to the right field and creating masters as needed.\n"
			"Example: a patient submits the enrolment web form; a new lead is created with their "
			"details, program, and attachments already filled in."
		),
		backs=["tatva_connect.intake.intake.process_submission"],
	),
	Auto(
		key="Partner::Catalog::cache",
		fires_on="Doc Event",
		trigger_detail="CRM Lead API Field · on_update + on_trash",
		purpose=(
			"Refreshes the partner-API field catalog the moment its definition changes, so "
			"external partners always fetch the current list of fields.\n"
			"Example: an admin adds a new API field; the partner's fetched catalog reflects it on "
			"the next call instead of serving a stale list."
		),
		backs=["tatva_connect.api.partner.clear_catalog_cache"],
	),
	Auto(
		key="Observability::Metrics::rollup",
		fires_on="Schedule",
		trigger_detail="every 6h",
		purpose=(
			"Rolls the raw request log up into compact metric tables every 6 hours to power "
			"dashboards and health checks without bloating the database.\n"
			"Example: the operations dashboard's request-volume and latency charts refresh on "
			"this schedule."
		),
		backs=["tatva_connect.observability.rollup.run"],
	),
]
