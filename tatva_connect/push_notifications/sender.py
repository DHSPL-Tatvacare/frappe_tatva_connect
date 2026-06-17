"""FCM HTTP v1 sender — straight to Firebase, no relay.

google-auth mints a short-lived OAuth2 access token from the service-account JSON
stored (encrypted) on CRM Push Settings; we cache it ~50m and POST one device token
per call to messages:send. Stale tokens (UNREGISTERED / 404) are pruned so the
subscription table self-heals.

Dormant by design: the master switch lives in the automation registry
(`Push::FCM::notify`, a Toggle that ships OFF) and the settings form ships blank —
nothing leaves the box until an operator enables the row AND fills the form.

google-auth is imported lazily inside the mint step so the app still loads if the
dependency is ever missing on a bench (the send just logs and no-ops).
"""
import base64
import json

import frappe
import requests

from tatva_connect import automation

SETTINGS = "CRM Push Settings"
SUBSCRIPTION = "CRM Push Subscription"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_ENDPOINT = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
_ACCESS_TOKEN_CACHE_KEY = "push_fcm_access_token"


def is_enabled() -> bool:
	"""Master kill-switch — the operator-togglable registry row. Fresh DB read so a
	flip takes effect at once; an unknown/dormant key reads False (fail-closed)."""
	return automation.is_enabled("Push::FCM::notify")


def _service_account_info():
	"""Parse the service-account JSON from the (encrypted) settings field. Accepts raw
	minified JSON or base64-of-JSON. Returns the dict, or None if blank/unparseable."""
	raw = (frappe.get_cached_doc(SETTINGS).get_password("service_account_json", raise_exception=False) or "").strip()
	if not raw:
		return None
	try:
		return json.loads(raw)
	except Exception:
		try:
			return json.loads(base64.b64decode(raw))
		except Exception:
			frappe.log_error("service_account_json is neither valid JSON nor base64-JSON", "Push Notifications")
			return None


def _access_token(info) -> str | None:
	"""OAuth2 bearer for FCM, cached ~50m (google tokens last 60m)."""
	cached = frappe.cache().get_value(_ACCESS_TOKEN_CACHE_KEY)
	if cached:
		return cached
	try:
		from google.auth.transport.requests import Request
		from google.oauth2 import service_account

		creds = service_account.Credentials.from_service_account_info(info, scopes=[FCM_SCOPE])
		creds.refresh(Request())
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Push Notifications: token mint failed")
		return None
	frappe.cache().set_value(_ACCESS_TOKEN_CACHE_KEY, creds.token, expires_in_sec=3000)
	return creds.token


def _post_one(project_id, access_token, fcm_token, title, body, data) -> requests.Response:
	message = {
		"message": {
			"token": fcm_token,
			"notification": {"title": title, "body": body},
			"data": {k: str(v) for k, v in (data or {}).items()},
			"webpush": {"fcm_options": {"link": (data or {}).get("route") or "/crm"}},
		}
	}
	return requests.post(
		FCM_ENDPOINT.format(project_id=project_id),
		headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
		data=json.dumps(message),
		timeout=10,
	)


def send_to_users(users, title, body, data=None):
	"""Enqueued entry point. Fan a notification out to every registered device of the
	given users. Silent no-op when disabled, unconfigured, or no devices."""
	users = [u for u in dict.fromkeys(users) if u and u != "Guest"]
	if not users or not is_enabled():
		return
	info = _service_account_info()
	if not info or not info.get("project_id"):
		return
	subs = frappe.get_all(SUBSCRIPTION, filters={"user": ["in", users]}, fields=["name", "fcm_token"])
	if not subs:
		return
	access_token = _access_token(info)
	if not access_token:
		return

	project_id = info["project_id"]
	for sub in subs:
		try:
			resp = _post_one(project_id, access_token, sub.fcm_token, title, body, data)
			if resp.status_code == 200:
				continue
			# 404 / UNREGISTERED => the device unsubscribed or the token rotated. Prune it.
			if resp.status_code == 404 or "UNREGISTERED" in resp.text:
				frappe.delete_doc(SUBSCRIPTION, sub.name, ignore_permissions=True, force=True)
			else:
				frappe.log_error(f"FCM send failed [{resp.status_code}]: {resp.text[:300]}", "Push Notifications")
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Push Notifications: send")
