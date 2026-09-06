"""The ONE notification catalog — every notifiable moment is one row here.

A event is a registered moment a rep can be told about (a lead assigned, a task
assigned, …). Each event is gated twice: the operator's GLOBAL switch
(`is_enabled(automation_key)` — the ONE gate, owned by the automation plane) and the
rep's per-user OPT-IN (default OFF). Dispatch, the prefs panel, and opt-in storage all
DERIVE from this catalog — nothing is special-cased per event in code.

Mirrors `automation/registry.py`: a frozen dataclass + a flat list + a `get()` accessor.
`automation_key` MUST name an `Auto` row in registry.py — the drift guard (drift.py)
fails `bench migrate` if it doesn't.
"""
from dataclasses import dataclass

CHANNELS = ("live",)  # the ONLY channel this plane delivers; per-user EMAIL prefs are a view onto frappe's own Notification Settings (notifications/api.py, shipped) — never add "email" here.


@dataclass(frozen=True)
class NotifiableEvent:
	key: str             # "Lead::Assignment::assigned"  (Area::Source::event)
	label: str           # shown in the prefs panel
	description: str      # one line, user-facing
	automation_key: str   # the CRM Tatva Automation row that globally enables it (the ONE gate)
	channels: tuple       # subset of CHANNELS
	default_optin: bool   # push events ship False — explicit opt-in
	urgency: str          # "presence_routed" (default) | "always_push"
	source: str           # "tatva" (we send) — what every event is today; "core" is reserved for a moment frappe itself raises
	bell_type: str = ""   # crm CRM Notification `type` when WE must write the bell row; "" when crm already writes one (assignment, inbound WhatsApp) — never both


EVENTS = [
	NotifiableEvent(
		key="Lead::Assignment::assigned",
		label="Lead assigned to me",
		description="A new lead is assigned to you.",
		automation_key="Notify::Lead::assigned",
		channels=("live",),
		default_optin=False,
		urgency="presence_routed",
		source="tatva",
	),
	NotifiableEvent(
		key="Task::Assignment::assigned",
		label="Task assigned to me",
		description="A new task is assigned to you.",
		automation_key="Notify::Task::assigned",
		channels=("live",),
		default_optin=False,
		urgency="presence_routed",
		source="tatva",
	),
	NotifiableEvent(
		key="WhatsApp::Message::received",
		label="Patient replied on WhatsApp",
		description="A patient sends a WhatsApp message on one of your leads.",
		automation_key="Notify::WhatsApp::received",
		channels=("live",),
		default_optin=False,
		urgency="presence_routed",
		source="tatva",
	),
	NotifiableEvent(
		key="Telephony::Call::missed",
		label="Missed call from a patient",
		description="A patient called one of your leads and the call went unanswered.",
		automation_key="Notify::Telephony::missed",
		channels=("live",),
		default_optin=False,
		urgency="always_push",
		source="tatva",
		bell_type="Call",
	),
	NotifiableEvent(
		key="Task::Due::soon",
		label="Task due soon",
		description="A task assigned to you falls due within the operator's lead time.",
		automation_key="Notify::Task::due-soon",
		channels=("live",),
		default_optin=False,
		urgency="presence_routed",
		source="tatva",
		bell_type="Task",
	),
	NotifiableEvent(
		key="Task::Due::overdue",
		label="Task overdue",
		description="A task assigned to you passed its due date and is not done.",
		automation_key="Notify::Task::overdue",
		channels=("live",),
		default_optin=False,
		urgency="presence_routed",
		source="tatva",
		bell_type="Task",
	),
	NotifiableEvent(
		key="Lead::Stage::changed",
		label="Lead stage changed",
		description="The stage of a lead assigned to you is moved.",
		automation_key="Notify::Lead::stage-changed",
		channels=("live",),
		default_optin=False,
		urgency="presence_routed",
		source="tatva",
		bell_type="Lead",
	),
]

_BY_KEY = {g.key: g for g in EVENTS}


def get(key: str) -> NotifiableEvent | None:
	"""Resolve a event by key — None for an unknown key (dispatch fails closed on None)."""
	return _BY_KEY.get(key)


def all_events() -> list:
	return list(EVENTS)
