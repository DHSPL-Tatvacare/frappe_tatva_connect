"""The ONE catalog — every automation in `tatva_connect` is one row here.

Every automation is an operator toggle, gated via `is_enabled(key)`: ON -> the
tatva_connect hook rides and enforces; OFF -> it's dormant and native frappe/crm
behaviour stands. No locked/always-on class — the operator owns every `enabled`.

`fires_on`: Doc Event · Schedule · Provider call.
`Area` is derived — segment 1 of the composite key (`key.split("::")[0]`).
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
	backs: list = field(default_factory=list)
	requires: str = ""


AUTOMATIONS = [
	Auto(
		key="WhatsApp::WATI::messaging",
		fires_on="Provider call",
		trigger_detail="whatsapp/api gate · WhatsApp Message · before_save",
		backs=["tatva_connect.whatsapp.webhook.pin_inbound_reference"],
	),
	Auto(
		key="WhatsApp::WATI::templates",
		fires_on="Schedule",
		trigger_detail="every 6h",
		backs=["tatva_connect.whatsapp.templates_sync.scheduled_sync_all"],
		requires="WhatsApp::WATI::messaging",
	),
	Auto(
		key="Telephony::Acefone::calls",
		fires_on="Provider call",
		trigger_detail="telephony/api gate",
		backs=[],
	),
	Auto(
		key="Storage::Azure::offload",
		fires_on="Doc Event",
		trigger_detail="File · after_insert + on_trash",
		backs=[
			"tatva_connect.storage.file_events.after_insert",
			"tatva_connect.storage.file_events.on_trash",
		],
	),
	Auto(
		key="Location::Google::capture",
		fires_on="Provider call",
		trigger_detail="location/api gate",
		backs=[],
	),
	Auto(
		key="Task::CRM Task::transitions",
		fires_on="Doc Event",
		trigger_detail="CRM Task · on_update",
		backs=["tatva_connect.activity.automation.apply_transitions"],
	),
	Auto(
		key="Task::Assignment::followup",
		fires_on="Doc Event",
		trigger_detail="ToDo · after_insert · WhatsApp Message · after_insert",
		backs=[
			"tatva_connect.tasks.tasks.on_lead_assignment",
			"tatva_connect.whatsapp.inbound.on_inbound_message",
		],
	),
	Auto(
		key="Storage::File::draft-cleanup",
		fires_on="Schedule",
		trigger_detail="daily 02:30",
		backs=["tatva_connect.api.email.purge_draft_attachments"],
	),
	Auto(
		key="Push::FCM::notify",
		fires_on="Doc Event",
		trigger_detail="CRM Task · after_insert · ToDo · after_insert",
		backs=[
			"tatva_connect.push_notifications.events.on_task_created",
			"tatva_connect.push_notifications.events.on_lead_assigned",
		],
	),
	Auto(
		key="Lead::CRM Lead::dedup",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · before_validate + validate",
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
		backs=["tatva_connect.lead.leads.validate_stage"],
	),
	Auto(
		key="Task::CRM Task::guards",
		fires_on="Doc Event",
		trigger_detail="CRM Task · validate",
		backs=[
			"tatva_connect.tasks.tasks.seed_checklist",
			"tatva_connect.tasks.tasks.enforce_checklist",
			"tatva_connect.tasks.tasks.enforce_location",
			"tatva_connect.tasks.tasks.enforce_activity_logged",
		],
	),
	Auto(
		key="Storage::File::privacy",
		fires_on="Doc Event",
		trigger_detail="File · validate",
		backs=["tatva_connect.storage.file_events.apply_privacy_policy"],
	),
	Auto(
		key="Lead::CRM Lead::headline",
		fires_on="Doc Event",
		trigger_detail="CRM Lead · validate",
		backs=["tatva_connect.lead.leads.sync_headline_metrics"],
	),
	Auto(
		key="Lead::Enrolment::intake",
		fires_on="Doc Event",
		trigger_detail="CRM Enrolment Submission · after_insert",
		backs=["tatva_connect.intake.intake.process_submission"],
	),
	Auto(
		key="Partner::Catalog::cache",
		fires_on="Doc Event",
		trigger_detail="CRM Lead API Field · on_update + on_trash",
		backs=["tatva_connect.api.partner.clear_catalog_cache"],
	),
	Auto(
		key="Observability::Metrics::rollup",
		fires_on="Schedule",
		trigger_detail="every 6h",
		backs=["tatva_connect.observability.rollup.run"],
	),
]
