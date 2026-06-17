"""The ONE catalog — every automation in `tatva_connect` is one row here.

`kind`: Switch (operator-togglable, gated via `is_enabled`) · Guard (fail-closed,
always runs, locked ON) · Infra (always runs, listed, not togglable).
`backs` = the real dotted paths the row covers; the drift check (drift.py) matches
hooks.py against these strings, so a new doc_event/scheduler path with no registry
row fails `bench migrate`.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Auto:
	key: str
	label: str
	entity: str
	kind: str
	trigger: str
	trigger_detail: str
	backs: list = field(default_factory=list)
	requires: str = ""


AUTOMATIONS = [
	# --- Switches (operator-togglable, gated) ---
	Auto(
		key="wati",
		label="WhatsApp (WATI)",
		entity="WhatsApp",
		kind="Switch",
		trigger="Integration",
		trigger_detail="whatsapp/api gate · WhatsApp Message · before_save",
		backs=["tatva_connect.whatsapp.webhook.pin_inbound_reference"],
	),
	Auto(
		key="template_sync",
		label="Template sync",
		entity="WhatsApp",
		kind="Switch",
		trigger="Scheduled",
		trigger_detail="every 6h",
		backs=["tatva_connect.whatsapp.templates_sync.scheduled_sync_all"],
		requires="wati",
	),
	Auto(
		key="acefone",
		label="Telephony (Acefone)",
		entity="Telephony",
		kind="Switch",
		trigger="Integration",
		trigger_detail="telephony/api gate",
		backs=[],
	),
	Auto(
		key="azure",
		label="Azure file offload",
		entity="Storage",
		kind="Switch",
		trigger="Doc Event",
		trigger_detail="File · after_insert + on_trash",
		backs=[
			"tatva_connect.storage.file_events.after_insert",
			"tatva_connect.storage.file_events.on_trash",
		],
	),
	Auto(
		key="location",
		label="Location capture",
		entity="Location",
		kind="Switch",
		trigger="Integration",
		trigger_detail="location/api gate",
		backs=[],
	),
	Auto(
		key="activity_transitions",
		label="Activity transitions",
		entity="Tasks",
		kind="Switch",
		trigger="Doc Event",
		trigger_detail="CRM Task · on_update",
		backs=["tatva_connect.activity.automation.apply_transitions"],
	),
	Auto(
		key="followup",
		label="Follow-up tasks",
		entity="Tasks",
		kind="Switch",
		trigger="Doc Event",
		trigger_detail="ToDo · after_insert · WhatsApp Message · after_insert",
		backs=[
			"tatva_connect.tasks.tasks.on_lead_assignment",
			"tatva_connect.whatsapp.inbound.on_inbound_message",
		],
	),
	Auto(
		key="draft_cleanup",
		label="Draft-attachment cleanup",
		entity="System",
		kind="Switch",
		trigger="Scheduled",
		trigger_detail="daily 02:30",
		backs=["tatva_connect.api.email.purge_draft_attachments"],
	),
	# --- Guards (fail-closed, always run, locked ON, visible) ---
	Auto(
		key="lead_hygiene",
		label="Lead dedup & hygiene",
		entity="Leads",
		kind="Guard",
		trigger="Doc Event",
		trigger_detail="CRM Lead · before_validate + validate",
		backs=[
			"tatva_connect.lead.leads.canonicalize_routing_fields",
			"tatva_connect.lead.leads.normalize_lead_phones",
			"tatva_connect.lead.leads.dedup_guard",
			"tatva_connect.lead.leads.validate_stage",
		],
	),
	Auto(
		key="task_guards",
		label="Checklist + location guards",
		entity="Tasks",
		kind="Guard",
		trigger="Doc Event",
		trigger_detail="CRM Task · validate",
		backs=[
			"tatva_connect.tasks.tasks.seed_checklist",
			"tatva_connect.tasks.tasks.enforce_checklist",
			"tatva_connect.tasks.tasks.enforce_location",
			"tatva_connect.tasks.tasks.enforce_activity_logged",
		],
	),
	Auto(
		key="file_privacy",
		label="File privacy policy",
		entity="Storage",
		kind="Guard",
		trigger="Doc Event",
		trigger_detail="File · validate",
		backs=["tatva_connect.storage.file_events.apply_privacy_policy"],
	),
	# --- Infra (always run, listed, not togglable) ---
	Auto(
		key="headline_sync",
		label="Lead headline sync",
		entity="Leads",
		kind="Infra",
		trigger="Doc Event",
		trigger_detail="CRM Lead · validate",
		backs=["tatva_connect.lead.leads.sync_headline_metrics"],
	),
	Auto(
		key="intake",
		label="Intake processing",
		entity="Leads",
		kind="Infra",
		trigger="Doc Event",
		trigger_detail="CRM Enrolment Submission · after_insert",
		backs=["tatva_connect.intake.intake.process_submission"],
	),
	Auto(
		key="catalog_cache",
		label="Partner-catalog cache bust",
		entity="System",
		kind="Infra",
		trigger="Doc Event",
		trigger_detail="CRM Lead API Field · on_update + on_trash",
		backs=["tatva_connect.api.partner.clear_catalog_cache"],
	),
]
