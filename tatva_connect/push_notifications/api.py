"""Whitelisted endpoints the CRM SPA calls — register/unregister this browser's FCM
token, and fetch the public Web Push config. All run as the logged-in rep; the
subscription rows are written with ignore_permissions so the doctype itself stays
System-Manager-only (reps never list other reps' devices)."""
import frappe
from frappe.utils import now_datetime

SETTINGS = "CRM Push Settings"
SUBSCRIPTION = "CRM Push Subscription"


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
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc(
			{
				"doctype": SUBSCRIPTION,
				"user": user,
				"fcm_token": fcm_token,
				"device_label": device_label,
				"last_seen": now_datetime(),
			}
		).insert(ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist()
def unregister_token(fcm_token):
	"""Drop this device's subscription (rep revoked permission / logged out)."""
	name = frappe.db.get_value(SUBSCRIPTION, {"fcm_token": fcm_token}, "name")
	if name:
		frappe.delete_doc(SUBSCRIPTION, name, ignore_permissions=True, force=True)
	return {"ok": True}


@frappe.whitelist()
def get_web_config():
	"""Public Firebase web config + VAPID key for the browser SDK (not secret).
	`enabled` is true only once the operator has filled the form."""
	s = frappe.get_cached_doc(SETTINGS)
	return {
		"apiKey": s.web_api_key,
		"authDomain": s.web_auth_domain,
		"projectId": s.web_project_id,
		"messagingSenderId": s.web_messaging_sender_id,
		"appId": s.web_app_id,
		"vapidKey": s.vapid_key,
		"enabled": bool(s.web_api_key and s.vapid_key),
	}
