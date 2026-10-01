"""The ONE notification catalog: each event is gated by MASTER, by its `field` on ORG_SETTINGS (operator) and by the same `field` on USER_SETTINGS (rep); labels live on those fields."""
from dataclasses import dataclass

CHANNELS = ("live",)  # the ONLY channel this plane delivers; per-user EMAIL prefs are frappe's own Notification Settings fields (notifications/api.py) — never add "email" here.

MASTER = "Notify::Push::live"
ORG_SETTINGS = "CRM Notification Settings"
USER_SETTINGS = "Notification Settings"
USER_PUSH_MASTER = "enable_push_notifications"  # the user's own push master on USER_SETTINGS, beside frappe's `enable_email_notifications`

LEAD_ASSIGNED = "Lead::Assignment::assigned"
WHATSAPP_RECEIVED = "WhatsApp::Message::received"
CALL_MISSED = "Telephony::Call::missed"
DUE_SOON = "Task::Due::soon"
OVERDUE = "Task::Due::overdue"


@dataclass(frozen=True)
class NotifiableEvent:
	key: str             # "Lead::Assignment::assigned"  (Area::Source::event)
	field: str           # the Check on ORG_SETTINGS (operator) and on USER_SETTINGS (rep) that gates it
	channels: tuple       # subset of CHANNELS
	urgency: str          # "presence_routed" (default) | "always_push"
	source: str           # "tatva" (we send) — what every event is today; "core" is reserved for a moment frappe itself raises
	bell_type: str = ""   # crm CRM Notification `type` when WE must write the bell row; "" when crm already writes one (assignment, inbound WhatsApp) — never both


EVENTS = [
	NotifiableEvent(
		key=LEAD_ASSIGNED,
		field="push_lead_assigned",
		channels=("live",),
		urgency="presence_routed",
		source="tatva",
	),
	NotifiableEvent(
		key=WHATSAPP_RECEIVED,
		field="push_whatsapp_received",
		channels=("live",),
		urgency="presence_routed",
		source="tatva",
	),
	NotifiableEvent(
		key=CALL_MISSED,
		field="push_call_missed",
		channels=("live",),
		urgency="always_push",
		source="tatva",
		bell_type="Call",
	),
	NotifiableEvent(
		key=DUE_SOON,
		field="push_due_soon",
		channels=("live",),
		urgency="presence_routed",
		source="tatva",
		bell_type="Task",
	),
	NotifiableEvent(
		key=OVERDUE,
		field="push_overdue",
		channels=("live",),
		urgency="presence_routed",
		source="tatva",
		bell_type="Task",
	),
]

_BY_KEY = {g.key: g for g in EVENTS}


def get(key: str) -> NotifiableEvent | None:
	"""Resolve a event by key — None for an unknown key (dispatch fails closed on None)."""
	return _BY_KEY.get(key)


def all_events() -> list:
	return list(EVENTS)
