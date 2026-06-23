"""Whitelisted endpoints the CRM SPA calls — two concerns, one module:

  * Per-user prefs — a rep reads/writes ONLY their OWN opt-in row. The doctype stays
    System-Manager-only; these run as the session user and write with ignore_permissions
    scoped to that user (a rep never touches another's prefs). The panel lists EVERY
    catalog grain so reps see what exists; ones the operator hasn't globally enabled come
    back `available: False` (the panel greys + disables them, and save rejects changes to
    them — so a rep can never opt into a type the org switched off).
  * Device registration — register/unregister this browser's FCM token and fetch the
    public Web Push config the browser SDK needs (the FCM transport's enrolment).
"""
import json

import frappe
from frappe.utils import now_datetime

from tatva_connect.notifications import catalog

PREFERENCE = "CRM Notification Preference"
SETTINGS = "CRM Push Settings"
SUBSCRIPTION = "CRM Push Subscription"


# ── Per-user prefs ────────────────────────────────────────────────────────────────────


def _enabled_automation_keys() -> set:
	"""Every globally-enabled automation key — one batched read (no per-grain query)."""
	return set(frappe.get_all("CRM Tatva Automation", filters={"enabled": 1}, pluck="name"))


def _stored_optins(user) -> dict:
	"""{grain_key: enabled} from the user's row — absent row = no opt-ins (default OFF)."""
	name = frappe.db.exists(PREFERENCE, {"user": user})
	if not name:
		return {}
	rows = frappe.get_all(
		"CRM Notification Subscription",
		filters={"parenttype": PREFERENCE, "parent": name, "parentfield": "subscriptions"},
		fields=["grain_key", "enabled"],
		ignore_permissions=True,
	)
	return {r.grain_key: bool(r.enabled) for r in rows}


@frappe.whitelist()
def get_my_notification_prefs():
	"""One entry per catalog grain so reps see the full registry. `available` = the operator
	has globally enabled it; `enabled` = the rep's stored opt-in (falling back to default)."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	stored = _stored_optins(user)
	enabled_keys = _enabled_automation_keys()
	return [
		{
			"grain_key": g.key,
			"label": g.label,
			"description": g.description,
			"available": g.automation_key in enabled_keys,
			"enabled": stored.get(g.key, g.default_optin),
		}
		for g in catalog.all_grains()
	]


@frappe.whitelist()
def save_my_notification_prefs(prefs):
	"""Persist the rep's opt-ins onto their OWN row. `prefs` = [{grain_key, enabled}, …].
	Changes apply ONLY to globally-enabled grains (a greyed type can't be flipped from the
	panel, nor via a crafted payload); a disabled grain's existing opt-in is preserved so it
	returns intact if the operator re-enables it."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	if isinstance(prefs, str):
		prefs = json.loads(prefs)

	available = {g.key for g in catalog.all_grains() if g.automation_key in _enabled_automation_keys()}
	final = _stored_optins(user)  # start from what's stored (preserves disabled-grain opt-ins)
	for p in prefs:
		key = p.get("grain_key")
		if key in available:  # only operator-enabled grains are the rep's to change
			final[key] = bool(p.get("enabled"))

	known = {g.key for g in catalog.all_grains()}
	name = frappe.db.exists(PREFERENCE, {"user": user})
	doc = frappe.get_doc(PREFERENCE, name) if name else frappe.new_doc(PREFERENCE)
	doc.user = user
	doc.set("subscriptions", [])
	for grain_key, enabled in final.items():
		if grain_key in known:  # drop rows for retired grains
			doc.append("subscriptions", {"grain_key": grain_key, "channel": "live", "enabled": int(enabled)})
	doc.save(ignore_permissions=True)  # authz-ok: self-scoped — doc.user pinned to session.user; writes only the caller's own prefs row
	return {"ok": True}


# ── Device registration (FCM transport enrolment) ─────────────────────────────────────


@frappe.whitelist()
def register_token(fcm_token, device_label=None):
	"""Upsert one subscription row for (this user, this device token)."""
	user = frappe.session.user
	if not fcm_token or user == "Guest":
		return {"ok": False}

	name = frappe.db.get_value(SUBSCRIPTION, {"fcm_token": fcm_token}, "name")
	if name:
		doc = frappe.get_doc(SUBSCRIPTION, name)
		doc.user = user
		doc.device_label = device_label or doc.device_label
		doc.last_seen = now_datetime()
		doc.save(ignore_permissions=True)  # authz-ok: self-scoped — user pinned to session.user; writes only the caller's own device row
	else:
		frappe.get_doc(
			{
				"doctype": SUBSCRIPTION,
				"user": user,
				"fcm_token": fcm_token,
				"device_label": device_label,
				"last_seen": now_datetime(),
			}
		).insert(ignore_permissions=True)  # authz-ok: self-scoped — user pinned to session.user; creates only the caller's own device row
	return {"ok": True}


@frappe.whitelist()
def unregister_token(fcm_token):
	"""Drop this device's subscription (rep revoked permission / logged out). Scoped to the
	session user so a crafted token can only ever delete the caller's OWN device row."""
	name = frappe.db.get_value(SUBSCRIPTION, {"fcm_token": fcm_token, "user": frappe.session.user}, "name")
	if name:
		frappe.delete_doc(SUBSCRIPTION, name, ignore_permissions=True, force=True)  # authz-ok: self-scoped — name resolved with {user: session.user}, deletes only the caller's own device row
	return {"ok": True}


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
