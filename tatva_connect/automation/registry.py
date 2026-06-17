"""The ONE catalog — every automation in `tatva_connect` is one row here.

`control`: Toggle (operator-togglable, gated via `is_enabled`) · Always-on
(fail-closed safety / system row, always runs, locked ON).
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
	control: str
	fires_on: str
	lock_reason: str = ""
	trigger_detail: str = ""
	backs: list = field(default_factory=list)
	requires: str = ""


AUTOMATIONS = [
	# --- Toggles (operator-togglable, gated) ---
	Auto(
		key="WhatsApp::WATI::messaging",
		control="Toggle",
		fires_on="Provider call",
		trigger_detail="whatsapp/api gate · WhatsApp Message · before_save",
		backs=["tatva_connect.whatsapp.webhook.pin_inbound_reference"],
	),
	Auto(
		key="WhatsApp::WATI::templates",
		control="Toggle",
		fires_on="Schedule",
		trigger_detail="every 6h",
		backs=["tatva_connect.whatsapp.templates_sync.scheduled_sync_all"],
		requires="WhatsApp::WATI::messaging",
	),
	Auto(
		key="Telephony::Acefone::calls",
		control="Toggle",
		fires_on="Provider call",
		trigger_detail="telephony/api gate",
		backs=[],
	),
	Auto(
		key="Storage::Azure::offload",
		control="Toggle",
		fires_on="Doc Event",
		trigger_detail="File · after_insert + on_trash",
		backs=[
			"tatva_connect.storage.file_events.after_insert",
			"tatva_connect.storage.file_events.on_trash",
		],
	),
	Auto(
		key="Location::Google::capture",
		control="Toggle",
		fires_on="Provider call",
		trigger_detail="location/api gate",
		backs=[],
	),
	Auto(
		key="Task::CRM Task::transitions",
		control="Toggle",
		fires_on="Doc Event",
		trigger_detail="CRM Task · on_update",
		backs=["tatva_connect.activity.automation.apply_transitions"],
	),
	Auto(
		key="Task::Assignment::followup",
		control="Toggle",
		fires_on="Doc Event",
		trigger_detail="ToDo · after_insert · WhatsApp Message · after_insert",
		backs=[
			"tatva_connect.tasks.tasks.on_lead_assignment",
			"tatva_connect.whatsapp.inbound.on_inbound_message",
		],
	),
	Auto(
		key="Storage::File::draft-cleanup",
		control="Toggle",
		fires_on="Schedule",
		trigger_detail="daily 02:30",
		backs=["tatva_connect.api.email.purge_draft_attachments"],
	),
	Auto(
		key="Push::FCM::notify",
		control="Toggle",
		fires_on="Doc Event",
		trigger_detail="CRM Task · after_insert · ToDo · after_insert",
		backs=[
			"tatva_connect.push_notifications.events.on_task_created",
			"tatva_connect.push_notifications.events.on_lead_assigned",
		],
	),
	# --- Always-on (fail-closed safety / system rows, locked ON, visible) ---
	Auto(
		key="Lead::CRM Lead::dedup",
		control="Always-on",
		fires_on="Doc Event",
		lock_reason="Safety",
		trigger_detail="CRM Lead · before_validate + validate",
		backs=[
			"tatva_connect.lead.leads.canonicalize_routing_fields",
			"tatva_connect.lead.leads.normalize_lead_phones",
			"tatva_connect.lead.leads.dedup_guard",
			"tatva_connect.lead.leads.validate_stage",
		],
	),
	Auto(
		key="Task::CRM Task::guards",
		control="Always-on",
		fires_on="Doc Event",
		lock_reason="Safety",
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
		control="Always-on",
		fires_on="Doc Event",
		lock_reason="Safety",
		trigger_detail="File · validate",
		backs=["tatva_connect.storage.file_events.apply_privacy_policy"],
	),
	Auto(
		key="Lead::CRM Lead::headline",
		control="Always-on",
		fires_on="Doc Event",
		lock_reason="System",
		trigger_detail="CRM Lead · validate",
		backs=["tatva_connect.lead.leads.sync_headline_metrics"],
	),
	Auto(
		key="Lead::Enrolment::intake",
		control="Always-on",
		fires_on="Doc Event",
		lock_reason="System",
		trigger_detail="CRM Enrolment Submission · after_insert",
		backs=["tatva_connect.intake.intake.process_submission"],
	),
	Auto(
		key="Partner::Catalog::cache",
		control="Always-on",
		fires_on="Doc Event",
		lock_reason="System",
		trigger_detail="CRM Lead API Field · on_update + on_trash",
		backs=["tatva_connect.api.partner.clear_catalog_cache"],
	),
]
