"""FCM HTTP v1 transport, a dumb sender: gating lives in push.drain; mints a cached OAuth token from CRM Push Settings, posts per token, prunes dead tokens; no-op while unconfigured."""
import base64
import functools
import json

import frappe
import requests  # ALLOWLIST 2026-06-29: FCM stays raw — caller prunes dead tokens via Response.status_code/.text; make_post_request raises on non-2xx + returns parsed JSON, breaking the prune. Do NOT convert.

SETTINGS = "CRM Push Settings"
SUBSCRIPTION = "CRM Push Subscription"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_ENDPOINT = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
_ACCESS_TOKEN_CACHE_KEY = "push_fcm_access_token"
_HTTP_TIMEOUT = 5  # FCM and Google OAuth answer in well under a second; a stall must not hold a short worker
_DEAD_TOKEN = ("UNREGISTERED", "SENDER_ID_MISMATCH")  # FCM errorCodes that never heal: the device left, or it belongs to another Firebase project
# A push older than this is stale news: the drain stops reaching for it, and FCM drops it undelivered, so a device that wakes late never replays a backlog.
STALE_SECONDS = 300


def configured() -> bool:
	"""Is push set up enough to try — the browser config names a project; a cached read, no decryption."""
	return bool(frappe.get_cached_doc(SETTINGS).get("web_project_id"))


def _service_account_info():
	"""The service-account JSON (raw or base64) from the encrypted settings field; None when blank or unparseable."""
	raw = (frappe.get_cached_doc(SETTINGS).get_password("service_account_json", raise_exception=False) or "").strip()
	if not raw:
		return None
	try:
		return json.loads(raw)  # ALLOWLIST 2026-06-29: keep raw — parse_json won't raise, would dead-path the base64 fallback below.
	except Exception:
		try:
			return json.loads(base64.b64decode(raw))  # ALLOWLIST 2026-06-29: base64-of-JSON fallback; parse_json won't raise.
		except Exception:
			frappe.log_error("service_account_json is neither valid JSON nor base64-JSON", "Notifications")
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
		creds.refresh(functools.partial(Request(), timeout=_HTTP_TIMEOUT))
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Notifications: token mint failed")
		return None
	frappe.cache().set_value(_ACCESS_TOKEN_CACHE_KEY, creds.token, expires_in_sec=3000)
	return creds.token


def _post_one(project_id, access_token, fcm_token, title, body, data, ttl) -> requests.Response:
	# Data-only: the service worker renders the one banner; a `notification` block would show a second.
	payload = {"title": title, "body": body}
	payload.update({k: str(v) for k, v in (data or {}).items()})
	message = {
		"message": {
			"token": fcm_token,
			"data": payload,
			# TTL is how long FCM may hold it for an unreachable device; high urgency reaches a device in battery saver (FCM webpush headers).
			"webpush": {
				"headers": {"TTL": str(ttl), "Urgency": "high"},
				"fcm_options": {"link": (data or {}).get("route") or "/crm"},
			},
		}
	}
	return requests.post(
		FCM_ENDPOINT.format(project_id=project_id),
		headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
		json=message,
		timeout=_HTTP_TIMEOUT,
	)


# `send_to_users` archived in .archive/notifications-one-settings-2026-10-02: every send goes through push.drain or the test push.


def devices(users) -> dict:
	"""{user: their FCM tokens}, in one read — the ONE token reader."""
	found = {}
	for row in frappe.get_all(SUBSCRIPTION, filters={"user": ["in", list(users)]}, fields=["user", "fcm_token"]):
		found.setdefault(row.user, []).append(row.fcm_token)
	return found


def credentials() -> tuple | None:
	"""(project_id, OAuth bearer) for a run of sends, the key decrypted once; None while push cannot send."""
	info = _service_account_info()
	if not info or not info.get("project_id"):
		return None
	access_token = _access_token(info)
	return (info["project_id"], access_token) if access_token else None


def send_to_tokens(tokens, title, body, data=None, creds=None, ttl=STALE_SECONDS):
	"""Push to the given device tokens with `creds` from `credentials()`, read here when not passed, alive for `ttl` seconds; a silent no-op when unconfigured or empty."""
	tokens = [t for t in dict.fromkeys(tokens) if t]
	if not tokens:
		return
	creds = creds or credentials()
	if not creds:
		return

	project_id, access_token = creds
	failed = []
	for token in tokens:
		try:
			resp = _post_one(project_id, access_token, token, title, body, data, ttl)
			if resp.status_code == 200:
				continue
			if resp.status_code == 401:
				frappe.cache.delete_value(_ACCESS_TOKEN_CACHE_KEY)  # a revoked or rotated key: the next job mints afresh
			if resp.status_code == 404 or any(code in resp.text for code in _DEAD_TOKEN):
				name = frappe.db.get_value(SUBSCRIPTION, {"fcm_token": token}, "name")
				if name:
					frappe.delete_doc(SUBSCRIPTION, name, ignore_permissions=True, force=True)  # authz-ok: tier-a — notification fan-out, background worker
			else:
				failed.append(f"[{resp.status_code}] {resp.text[:300]}")
		except Exception:
			failed.append(frappe.get_traceback())
	if failed:
		frappe.log_error(f"FCM send failed for {len(failed)} of {len(tokens)} device(s)", "\n\n".join(failed[:5]))
