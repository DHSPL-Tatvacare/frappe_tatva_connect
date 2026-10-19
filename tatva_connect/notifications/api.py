"""Whitelisted endpoints the CRM SPA calls: the rep's own notification settings (system, email, push on frappe's per-user Notification Settings) and this browser's FCM enrolment."""
import json

import frappe
from frappe.utils import cint, now_datetime

from tatva_connect.notifications import dispatch, sender
from tatva_connect.utils import spend_rate_limit

SETTINGS = "CRM Push Settings"
SUBSCRIPTION = "CRM Push Subscription"

_RATE_LIMIT_MESSAGE = "Too many requests. Please try again in a minute."


def _throttle(counter, limit):
	"""Rate-limit per signed-in caller (frappe's decorator keys on IP, one budget per office line); silent outside a request."""
	if frappe.request:
		spend_rate_limit(f"push-rl:{counter}", frappe.session.user, limit, 60, _RATE_LIMIT_MESSAGE)



# ── The user's notification settings: ONE row, frappe's own per-user `Notification Settings` ──────────

USER_SETTINGS = "Notification Settings"
_MASTER = "enabled"  # frappe's own master: off, no Notification Log is written, so no bell, email or push
_EMAIL_MASTER = "enable_email_notifications"
PUSH_MASTER = "enable_push_notifications"  # ours, beside frappe's email master
# A channel's types are a list of Notification Types on the user's row: frappe's own for email, the same shape for push.
EMAIL_TYPES_FIELD, PUSH_TYPES_FIELD = "email_notification_types", "push_notification_types"
LEAD_FIELD = "due_soon_lead"  # the rep's own task-reminder lead time, its options read as `N unit(s)`
LEAD_DEFAULT_MINUTES = 60  # a rep who never picked one hears an hour ahead
_MINUTES_PER = {"minute": 1, "hour": 60, "day": 1440}
_TYPE_KEY = "{field}::{type}"  # a cell's key in the SPA payload, so one type on two channels never collides
# The CRM's notices, in the app's wording; Helpdesk's own types stay out of this grid.
_TYPES = (
	("Assignment", "Assignment", "A lead, task or ticket is assigned to you."),
	("Mention", "Mention", "A colleague tags you in a comment."),
	("Share", "Share", "A colleague shares a lead or a view with you."),
	("WhatsApp", "WhatsApp message", "A patient writes on one of your leads."),
	(dispatch.MISSED_CALL, "Missed call", "One of your leads called and nobody answered."),
	(dispatch.TASK_DUE_SOON, "Task due soon", "A task of yours is about to fall due."),
	(dispatch.TASK_OVERDUE, "Task overdue", "A task of yours is past its due date."),
)
# A feature with no Notification Type keeps its own email checkbox (frappe's `is_email_enabled_for_feature`); it has no push.
_EMAIL_FEATURES = (("enable_email_event_reminders", "Meeting reminder", "Before a meeting you are invited to."),)


def _my_notification_settings(for_update=False):
	"""The caller's own row, created on first read so the doctype defaults decide its ship-state."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	if not frappe.db.exists(USER_SETTINGS, user):
		from frappe.desk.doctype.notification_settings.notification_settings import (
			create_notification_settings,
		)

		create_notification_settings(user)
	return frappe.get_doc(USER_SETTINGS, user, for_update=for_update)


def _switch(key, label, description, enabled):
	return {"fieldname": key, "label": frappe._(label), "description": frappe._(description), "enabled": bool(enabled)}


def _cell(key, enabled):
	return {"fieldname": key, "enabled": bool(enabled)}


def _chosen(doc, field) -> set:
	return {r.notification_type for r in doc.get(field)}


def _grid(doc):
	"""One row per CRM notice, with an email and a push cell; a notice we write is `available` only while the switch is on."""
	emailed, pushed, armed = _chosen(doc, EMAIL_TYPES_FIELD), _chosen(doc, PUSH_TYPES_FIELD), dispatch.armed()
	rows = [
		{
			"type": t, "label": frappe._(label), "description": frappe._(desc), "available": armed or t not in dispatch.TYPES,
			"email": _cell(_TYPE_KEY.format(field=EMAIL_TYPES_FIELD, type=t), t in emailed),
			"push": _cell(_TYPE_KEY.format(field=PUSH_TYPES_FIELD, type=t), t in pushed),
		}
		for t, label, desc in _TYPES
	]
	features = [
		{"type": f, "label": frappe._(label), "description": frappe._(desc), "available": True, "email": _cell(f, doc.get(f)), "push": None}
		for f, label, desc in _EMAIL_FEATURES
	]
	return rows + features


def minutes(option):
	"""Minutes an `N unit(s)` option spells (`1 hour` -> 60); None for any other wording."""
	number, _sep, unit = (option or "").partition(" ")
	per = _MINUTES_PER.get(unit.rstrip("s"))
	return cint(number) * per if number.isdigit() and per else None


def lead_options() -> list:
	"""The lead times the field itself declares, in order."""
	return [o for o in (frappe.get_meta(USER_SETTINGS).get_field(LEAD_FIELD).options or "").split("\n") if o]


def _lead(doc):
	"""The rep's lead time, shown as the option in effect: a rep who never picked one sees the hour they get."""
	options = lead_options()
	default = next((o for o in options if minutes(o) == LEAD_DEFAULT_MINUTES), "")
	return {"fieldname": LEAD_FIELD, "value": doc.get(LEAD_FIELD) or default, "options": [{"label": frappe._(o), "value": o} for o in options]}


@frappe.whitelist()
def get_my_notification_settings():
	"""The caller's masters, the notice × channel grid and their reminder lead time — one row, one read."""
	doc = _my_notification_settings()
	return {
		"master": _switch(_MASTER, "System notifications", "Your bell in the CRM, and every email and push alert sent from it.", doc.get(_MASTER)),
		"email": _switch(_EMAIL_MASTER, "Email", "Send the notices you tick below to your email.", doc.get(_EMAIL_MASTER)),
		"push": {
			**_switch(PUSH_MASTER, "Push", "Send the notices you tick below as alerts to your phone and desktop.", doc.get(PUSH_MASTER)),
			"available": dispatch.armed() and sender.configured(),
		},
		"grid": _grid(doc),
		"lead": _lead(doc),
	}


@frappe.whitelist(methods=["POST"])
def save_my_notification_settings(values):
	"""Write only the switches sent onto the caller's OWN row, locked so rapid toggles queue rather than collide, and answer with the row as stored."""
	if isinstance(values, str):
		values = json.loads(values)  # ALLOWLIST 2026-08-15: keep raw — surfaces a clean error on a malformed payload; parse_json won't raise.
	values = values or {}
	doc = _my_notification_settings(for_update=True)
	for fieldname in {_MASTER, _EMAIL_MASTER, PUSH_MASTER, *(f for f, _, _ in _EMAIL_FEATURES)} & values.keys():
		doc.set(fieldname, int(bool(values[fieldname])))
	if LEAD_FIELD in values:
		doc.set(LEAD_FIELD, values[LEAD_FIELD] or None)  # the field's own Select validation refuses a value it does not offer
	for field in (EMAIL_TYPES_FIELD, PUSH_TYPES_FIELD):
		sent = {t: values[k] for t, _, _ in _TYPES if (k := _TYPE_KEY.format(field=field, type=t)) in values}
		if sent:  # a type outside the CRM grid (ticked in Desk) is kept as it was
			chosen = (_chosen(doc, field) - sent.keys()) | {t for t, on in sent.items() if on}
			doc.set(field, [{"notification_type": t} for t in sorted(chosen)])
	doc.save()  # authz-ok: tier-c — rides frappe's own Notification Settings.has_permission (own row only)
	return get_my_notification_settings()


# ── Device registration (FCM transport enrolment) ─────────────────────────────────────


@frappe.whitelist(methods=["POST"])
def register_token(fcm_token, device_label=None):
	"""Bind this browser's FCM token to the caller; a shared machine's token deliberately changes hands so the last rep's pushes stop."""
	_throttle("register", 20)
	user = frappe.session.user
	if not fcm_token or user == "Guest":
		return {"ok": False}

	name = frappe.db.get_value(SUBSCRIPTION, {"fcm_token": fcm_token}, "name")
	if name:
		doc = frappe.get_doc(SUBSCRIPTION, name)
		doc.user = user  # the browser changed hands; the row follows it (see the docstring)
		doc.device_label = device_label or doc.device_label
		doc.last_seen = now_datetime()
		doc.save(ignore_permissions=True)  # authz-ok: tier-c — the row is pinned to session.user; the previous owner is unsubscribed by the same write, never left pointing at a browser they no longer hold
	else:
		frappe.get_doc(
			{
				"doctype": SUBSCRIPTION,
				"user": user,
				"fcm_token": fcm_token,
				"device_label": device_label,
				"last_seen": now_datetime(),
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-c — self-scoped: user pinned to session.user; creates only the caller's own device row
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def unregister_token(fcm_token):
	"""Drop the caller's own device row for this token; scoped to the session user."""
	name = frappe.db.get_value(SUBSCRIPTION, {"fcm_token": fcm_token, "user": frappe.session.user}, "name")
	if name:
		frappe.delete_doc(SUBSCRIPTION, name, ignore_permissions=True, force=True)  # authz-ok: tier-c — self-scoped: name resolved with {user: session.user}, deletes only the caller's own device row
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def validate_push_config():
	"""Prove the push config against Firebase by minting a fresh OAuth token, the step every send makes."""
	_throttle("validate", 10)
	frappe.only_for("System Manager")
	settings = frappe.get_cached_doc(SETTINGS)
	report = {"ok": False, "checks": []}

	def check(label, passed, detail=""):
		report["checks"].append({"label": label, "passed": bool(passed), "detail": detail})
		return passed

	info = sender._service_account_info()
	if not check("Service account JSON parses", bool(info), "" if info else "Blank, or neither JSON nor base64-of-JSON."):
		return report
	check("Service account names a project", bool(info.get("project_id")), info.get("project_id") or "No project_id inside the key.")
	check("Service account identity", True, info.get("client_email") or "")

	frappe.cache().delete_value(sender._ACCESS_TOKEN_CACHE_KEY)
	token = sender._access_token(info)
	if not check("Firebase accepts the key (OAuth token minted)", bool(token), "" if token else "Firebase refused it — the key is wrong, revoked, or the project is disabled."):
		return report

	web_api_key = settings.get_password("web_api_key", raise_exception=False)
	vapid = settings.get_password("vapid_key", raise_exception=False)
	for label, value in (
		("Web API Key", web_api_key),
		("Auth Domain", settings.web_auth_domain),
		("Project ID", settings.web_project_id),
		("Messaging Sender ID", settings.web_messaging_sender_id),
		("Web App ID", settings.web_app_id),
		("VAPID Key", vapid),
	):
		check(f"{label} filled", bool(value), "" if value else "The browser cannot register for push without it.")

	same_project = settings.web_project_id == info.get("project_id")
	check(
		"Browser config and service account name the SAME project",
		same_project,
		"" if same_project else f"Browser says '{settings.web_project_id}', the key says '{info.get('project_id')}' — a push would be refused as a sender mismatch.",
	)

	devices = frappe.db.count(SUBSCRIPTION, {"user": frappe.session.user})
	check(
		"This user has a registered device",
		devices > 0,
		f"{devices} device(s)." if devices else "Turn on 'Push notifications on this device' in the Notifications panel, or a push has nowhere to land.",
	)

	report["ok"] = all(c["passed"] for c in report["checks"])
	return report


@frappe.whitelist(methods=["POST"])
def send_test_push():
	"""Push a real message to the caller's own devices, through the SAME sender every event uses."""
	_throttle("test-send", 5)
	frappe.only_for("System Manager")
	tokens = sender.devices([frappe.session.user]).get(frappe.session.user)
	if not tokens:
		return {"ok": False, "sent": 0, "detail": "No device is registered for you — turn on 'Push notifications on this device' first."}
	sender.send_to_tokens(
		tokens,
		title="TatvaCare CRM",
		body="Test push — your Firebase credentials work.",
		data={"route": "/crm"},
	)
	return {"ok": True, "sent": len(tokens), "detail": f"Sent to {len(tokens)} device(s). Nothing arriving means the browser blocked notifications, or the device token is stale (it is pruned automatically)."}


@frappe.whitelist()
def get_web_config():
	"""The browser SDK's Firebase web config and VAPID key; `enabled` once the switch is on and both keys are filled, so no browser registers while push cannot send."""
	if not dispatch.armed():
		return {"enabled": False}
	s = frappe.get_cached_doc(SETTINGS)
	api_key = s.get_password("web_api_key", raise_exception=False)
	vapid_key = s.get_password("vapid_key", raise_exception=False)
	return {
		"apiKey": api_key,
		"authDomain": s.web_auth_domain,
		"projectId": s.web_project_id,
		"messagingSenderId": s.web_messaging_sender_id,
		"appId": s.web_app_id,
		"vapidKey": vapid_key,
		"enabled": bool(api_key and vapid_key),
	}
