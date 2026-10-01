"""Whitelisted endpoints the CRM SPA calls: the rep's own notification settings (system, email, push on frappe's per-user Notification Settings) and this browser's FCM enrolment."""
import json

import frappe
from frappe.utils import now_datetime

from tatva_connect.notifications import catalog, dispatch, sender
from tatva_connect.utils import spend_rate_limit

SETTINGS = "CRM Push Settings"
SUBSCRIPTION = "CRM Push Subscription"

_RATE_LIMIT_MESSAGE = "Too many requests. Please try again in a minute."


def _throttle(counter, limit):
	"""One push counter, through the app's one identity-keyed limiter, keyed on the CALLER.

	These three endpoints are signed in, so the caller has a name. frappe's own decorator keys on the
	request IP whatever else it is given, which on a shared office line is one budget for the whole
	floor — the reason `spend_rate_limit` exists. The refusal class is the same either way, so what a
	browser sees on a refusal does not change.

	Silent outside an HTTP request, exactly as the decorator was: a job or a test calling one of these
	directly is not a caller with a budget."""
	if frappe.request:
		spend_rate_limit(f"push-rl:{counter}", frappe.session.user, limit, 60, _RATE_LIMIT_MESSAGE)



# ── The user's notification settings: ONE row, frappe's own per-user `Notification Settings` ──────────

_MASTER = "enabled"  # frappe's own master: off, no Notification Log is written (so no email), and prefs skips the user for push
_EMAIL_MASTER = "enable_email_notifications"
_EMAIL_TYPES_FIELD = "email_notification_types"
# Notification Types: frappe emails one only if it is in the user's `email_notification_types` list (v16 allow-list).
_EMAIL_TYPES = (
	("Assignment", "When a lead or task is assigned to me", "An email for each lead or task assigned to you."),
	("Mention", "When someone tags me in a comment", "An email when a colleague mentions you."),
	("Share", "When someone shares a lead with me", "An email when a colleague shares a lead with you."),
)
# A feature with no Notification Type keeps its own checkbox (frappe's `is_email_enabled_for_feature`).
_EMAIL_FEATURES = (("enable_email_event_reminders", "Meeting reminders", "An email before a meeting you are invited to."),)


def _my_notification_settings(for_update=False):
	"""The caller's own row, created on first read so the doctype defaults decide its ship-state."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	if not frappe.db.exists(catalog.USER_SETTINGS, user):
		from frappe.desk.doctype.notification_settings.notification_settings import (
			create_notification_settings,
		)

		create_notification_settings(user)
	return frappe.get_doc(catalog.USER_SETTINGS, user, for_update=for_update)


def _row(key, label, description, enabled, available=True):
	return {"fieldname": key, "label": frappe._(label), "description": frappe._(description or ""), "enabled": bool(enabled), "available": available}


def _emailed_types(doc) -> set:
	return {r.notification_type for r in doc.get(_EMAIL_TYPES_FIELD)}


def _push_rows(doc):
	"""One row per catalog event, labelled by its own field; `available` = the operator has it switched on."""
	meta = frappe.get_meta(catalog.USER_SETTINGS)
	return [
		_row(e.field, meta.get_label(e.field), meta.get_field(e.field).description, doc.get(e.field), dispatch.armed(e.key))
		for e in catalog.all_events()
	]


@frappe.whitelist()
def get_my_notification_settings():
	"""The caller's master, email and push switches — one row, one read."""
	doc = _my_notification_settings()
	emailed = _emailed_types(doc)
	return {
		"master": _row(_MASTER, "All notifications", "Turn off to stop every email and phone alert.", doc.get(_MASTER)),
		"email": {
			"master": _row(_EMAIL_MASTER, "Email notifications", "Also send these to your email.", doc.get(_EMAIL_MASTER)),
			"rows": [_row(t, label, desc, t in emailed) for t, label, desc in _EMAIL_TYPES]
			+ [_row(f, label, desc, doc.get(f)) for f, label, desc in _EMAIL_FEATURES],
		},
		"push": {
			"master": _row(catalog.USER_PUSH_MASTER, "Push notifications", "Also send these as alerts to your phone and desktop.", doc.get(catalog.USER_PUSH_MASTER)),
			"rows": _push_rows(doc),
		},
	}


@frappe.whitelist(methods=["POST"])
def save_my_notification_settings(values):
	"""Write only the switches sent onto the caller's OWN row, locked so rapid toggles queue rather than collide, and answer with the row as stored."""
	if isinstance(values, str):
		values = json.loads(values)  # ALLOWLIST 2026-08-15: keep raw — surfaces a clean error on a malformed payload; parse_json won't raise.
	values = values or {}
	doc = _my_notification_settings(for_update=True)
	fields = {_MASTER, _EMAIL_MASTER, catalog.USER_PUSH_MASTER, *(f for f, _, _ in _EMAIL_FEATURES), *(r["fieldname"] for r in _push_rows(doc) if r["available"])}
	for fieldname in fields & values.keys():
		doc.set(fieldname, int(bool(values[fieldname])))
	types = {t for t, _, _ in _EMAIL_TYPES}
	if types & values.keys():
		chosen = (_emailed_types(doc) - types) | {t for t in types if values.get(t, t in _emailed_types(doc))}
		doc.set(_EMAIL_TYPES_FIELD, [{"notification_type": t} for t in sorted(chosen)])
	doc.save()  # authz-ok: tier-c — rides frappe's own Notification Settings.has_permission (own row only)
	return get_my_notification_settings()


# ── Device registration (FCM transport enrolment) ─────────────────────────────────────


@frappe.whitelist()
def register_token(fcm_token, device_label=None):
	"""Bind this browser's FCM token to the caller.

	A token identifies a BROWSER, not a person, and the SPA does not unregister on logout — so when a
	second rep signs in on a shared machine Firebase hands back the SAME token. The row therefore CHANGES
	HANDS to the caller, deliberately: leave it with the previous rep and their patient notifications
	would be pushed to a screen someone else is now sitting in front of. That is the leak this prevents.

	The takeover is not a hole to close: the token IS the capability — whoever holds it receives what is
	pushed to it — and nothing the server does can change that. It is protected by never being handed
	out (CRM Push Subscription is System Manager only, and the token is never logged); knowing one means
	already holding the browser. The cap here only stops a scripted sweep.
	"""
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


@frappe.whitelist()
def unregister_token(fcm_token):
	"""Drop this device's subscription (rep revoked permission / logged out). Scoped to the
	session user so a crafted token can only ever delete the caller's OWN device row."""
	name = frappe.db.get_value(SUBSCRIPTION, {"fcm_token": fcm_token, "user": frappe.session.user}, "name")
	if name:
		frappe.delete_doc(SUBSCRIPTION, name, ignore_permissions=True, force=True)  # authz-ok: tier-c — self-scoped: name resolved with {user: session.user}, deletes only the caller's own device row
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def validate_push_config():
	"""Is the push config real, and would a send actually work? Checked against Firebase, not guessed.

	The service-account key is exercised for real — an OAuth token is minted from it, which is the same
	step every send makes — so a wrong project, a revoked key or a malformed JSON is caught here rather
	than in a silent no-op at 2am. The cached token is dropped first, or a stale one would vouch for a
	credential that has since been replaced.
	"""
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
	tokens = frappe.get_all(SUBSCRIPTION, filters={"user": frappe.session.user}, pluck="fcm_token")
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
	"""Public Firebase web config + VAPID key for the browser SDK. The key + VAPID are stored
	as Password fields (masked in the form), so read them via get_password. `enabled` is true
	only once the operator has filled the form."""
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
