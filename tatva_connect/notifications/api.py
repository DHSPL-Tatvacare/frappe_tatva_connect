"""Whitelisted endpoints the CRM SPA calls — two concerns, one module:

  * Per-user prefs — a rep reads/writes ONLY their OWN opt-in row. The doctype stays
    System-Manager-only; these run as the session user and write with ignore_permissions
    scoped to that user (a rep never touches another's prefs). The panel lists EVERY
    catalog event so reps see what exists; ones the operator hasn't globally enabled come
    back `available: False` (the panel greys + disables them, and save rejects changes to
    them — so a rep can never opt into a type the org switched off).
  * Device registration — register/unregister this browser's FCM token and fetch the
    public Web Push config the browser SDK needs (the FCM transport's enrolment).
"""
import json

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime

from tatva_connect.notifications import catalog, sender

PREFERENCE = "CRM Notification Preference"
SETTINGS = "CRM Push Settings"
SUBSCRIPTION = "CRM Push Subscription"


# ── Per-user prefs ────────────────────────────────────────────────────────────────────


def _enabled_automation_keys() -> set:
	"""Every globally-enabled automation key — one batched read (no per-event query)."""
	return set(frappe.get_all("CRM Tatva Automation", filters={"enabled": 1}, pluck="name"))


def _stored_optins(user) -> dict:
	"""{event_key: enabled} from the user's row — absent row = no opt-ins (default OFF)."""
	name = frappe.db.exists(PREFERENCE, {"user": user})
	if not name:
		return {}
	rows = frappe.get_all(
		"CRM Notification Subscription",
		filters={"parenttype": PREFERENCE, "parent": name, "parentfield": "subscriptions"},
		fields=["event_key", "enabled"],
		ignore_permissions=True,  # authz-ok: tier-c — self-scoped: the write target is pinned to session.user
	)
	return {r.event_key: bool(r.enabled) for r in rows}


@frappe.whitelist()
def get_my_notification_prefs():
	"""One entry per catalog event so reps see the full registry. `available` = the operator
	has globally enabled it; `enabled` = the rep's stored opt-in (falling back to default)."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	stored = _stored_optins(user)
	enabled_keys = _enabled_automation_keys()
	return [
		{
			"event_key": g.key,
			"label": g.label,
			"description": g.description,
			"available": g.automation_key in enabled_keys,
			"enabled": stored.get(g.key, g.default_optin),
		}
		for g in catalog.all_events()
	]


@frappe.whitelist()
def save_my_notification_prefs(prefs):
	"""Persist the rep's opt-ins onto their OWN row. `prefs` = [{event_key, enabled}, …].
	Changes apply ONLY to globally-enabled events (a greyed type can't be flipped from the
	panel, nor via a crafted payload); a disabled event's existing opt-in is preserved so it
	returns intact if the operator re-enables it."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	if isinstance(prefs, str):
		prefs = json.loads(prefs)  # ALLOWLIST 2026-06-29: keep raw — surfaces a clean error on a malformed payload; parse_json won't raise.

	available = {g.key for g in catalog.all_events() if g.automation_key in _enabled_automation_keys()}
	final = _stored_optins(user)  # start from what's stored (preserves disabled-event opt-ins)
	for p in prefs:
		key = p.get("event_key")
		if key in available:  # only operator-enabled events are the rep's to change
			final[key] = bool(p.get("enabled"))

	known = {g.key for g in catalog.all_events()}
	name = frappe.db.exists(PREFERENCE, {"user": user})
	doc = frappe.get_doc(PREFERENCE, name) if name else frappe.new_doc(PREFERENCE)
	doc.user = user
	doc.set("subscriptions", [])
	for event_key, enabled in final.items():
		if event_key in known:  # drop rows for retired events
			doc.append("subscriptions", {"event_key": event_key, "channel": "live", "enabled": int(enabled)})
	doc.save(ignore_permissions=True)  # authz-ok: tier-c — self-scoped: doc.user pinned to session.user; writes only the caller's own prefs row
	return {"ok": True}


# ── Device registration (FCM transport enrolment) ─────────────────────────────────────


@frappe.whitelist()
@rate_limit(limit=20, seconds=60)
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
@rate_limit(limit=10, seconds=60)
def validate_push_config():
	"""Is the push config real, and would a send actually work? Checked against Firebase, not guessed.

	The service-account key is exercised for real — an OAuth token is minted from it, which is the same
	step every send makes — so a wrong project, a revoked key or a malformed JSON is caught here rather
	than in a silent no-op at 2am. The cached token is dropped first, or a stale one would vouch for a
	credential that has since been replaced.
	"""
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
@rate_limit(limit=5, seconds=60)
def send_test_push():
	"""Push a real message to the caller's own devices, through the SAME sender every event uses."""
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
