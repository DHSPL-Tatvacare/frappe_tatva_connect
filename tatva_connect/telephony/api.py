"""Acefone HTTP client + settings helpers.

Adapted from sanskar-onehash/crm_acefone_integration (MIT).

Thin wrapper over Acefone's REST API (https://api.acefone.in, version path
/v1/). Auth is a Bearer token that lives PER TENANT on a `CRM Telephony Account`
doc — this module is account-driven: every HTTP call takes the account whose creds
to use (resolved upstream by telephony/routing.py). The only GLOBAL state is the
`CRM Telephony Settings` kill-switch. We keep this module side-effect free: it never
writes a CRM Call Log — that lives in handler.py. Mirrors the kill-switch +
defensive-POST conventions of tatva_connect/wati/api.py.

Click-to-call returns `ref_id` synchronously and Acefone repeats it on the CDR, so that is the
correlation key. `custom_identifier` is NOT: it was empty on all 363 captured CDRs, and their docs type
it two ways (Object in the table, string in the OpenAPI block) — do not build on it.
"""
import re
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import make_get_request, make_post_request
from frappe.utils import cint

from tatva_connect import automation, phone

API_VERSION = "v1"
SETTINGS = "CRM Telephony Settings"
DEFAULT_BASE_URL = "https://api.acefone.in"


def is_enabled() -> bool:
	"""Acefone master kill-switch (GLOBAL). Dormant by default — OFF until explicitly
	enabled (a blank/unsaved single reads as disabled).

	Fresh DB read (not get_cached_doc) so flipping the switch takes effect
	immediately across all worker processes. Per-account `enabled` flags are
	checked by the routing/handler layer, not here.
	"""
	return automation.is_enabled("Telephony::Channel::calls")


def assert_enabled():
	"""Block outbound calls when the GLOBAL kill-switch is off."""
	if not is_enabled():
		frappe.throw(
			_("Acefone is disabled (CRM Telephony Settings → Enabled is off)."),
			title=_("Acefone disabled"),
		)


@frappe.whitelist()
def calls_enabled() -> dict:
	"""Whether the CRM may place calls — the one thing that draws the phone icon, read off OUR switch."""
	return {"enabled": is_enabled()}


def base_url_of(account) -> str:
	"""This account's base URL with any trailing slash stripped (or the default)."""
	base = (account.get("base_url") or DEFAULT_BASE_URL).strip()
	return base.rstrip("/")


def _headers(account) -> dict:
	"""Bearer auth from this CRM Telephony Account's api_token Password field."""
	token = account.get_password("api_token")
	return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _failure(exc) -> dict:
	"""Acefone's error body plus the transport outcome; without the status a 429 reads as a rejected number."""
	resp = getattr(frappe.flags, "integration_request", None)
	if resp is None:
		return {"success": False, "message": str(exc)[:400]}
	try:
		body = resp.json()
	except Exception:
		body = None
	if not isinstance(body, dict):
		body = {"success": False, "message": (getattr(resp, "text", "") or str(exc))[:400]}
	body["status_code"] = getattr(resp, "status_code", None)
	# An HTTP-date Retry-After reads as 0, so the caller falls back rather than promise a wait it cannot compute.
	body["retry_after"] = cint((getattr(resp, "headers", None) or {}).get("Retry-After")) or None
	return body


def succeeded(resp) -> bool:
	"""True only on a real success: `status_code` is stamped only by `_failure`, and Acefone sends `success` as a boolean AND as the STRING "false" (their rate-limit page)."""
	resp = resp or {}
	if resp.get("status_code") is not None:
		return False
	return str(resp.get("success", "")).strip().casefold() in ("true", "1")


def token_rejected(resp) -> bool:
	"""A 401/403 — the account's api_token is expired or wrong; Acefone's short-lived tokens die after 60 minutes."""
	return (resp or {}).get("status_code") in (401, 403)


def _post(account, endpoint: str, body: dict) -> dict:
	"""POST to Acefone (per-account) and ALWAYS return a parsed body.

	make_post_request raises on a 4xx/5xx; we catch it and surface Acefone's
	error body (or a synthetic one) so callers can show a clean message instead
	of a 500. Mirrors wati.api._post.
	"""
	url = f"{base_url_of(account)}/{API_VERSION}/{endpoint.lstrip('/')}"
	try:
		return make_post_request(url, headers=_headers(account), json=body)
	except Exception as e:
		return _failure(e)


def click_to_call(account, destination_number, agent_number, caller_id=None, custom_identifier=None) -> dict:
	"""POST /v1/click_to_call — rings the agent first, then bridges to the destination; `ref_id` correlates it."""
	# Acefone wants BARE DIGITS — a leading "+" is rejected. The single choke point for that.
	body = {
		"agent_number": phone.match_digits(agent_number),
		"destination_number": phone.match_digits(destination_number),
		"async": "1",
	}
	if caller_id:
		body["caller_id"] = phone.match_digits(caller_id)
	if custom_identifier:
		body["custom_identifier"] = str(custom_identifier)
	return _post(account, "click_to_call", body)


def _get(account, endpoint: str, params: dict) -> dict:
	"""GET from Acefone (per-account), always returning a parsed body.

	Mirrors `_post`: never raise on a non-2xx — surface Acefone's error body so the
	reconcile logs a clean message instead of a 500.
	"""
	query = urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
	url = f"{base_url_of(account)}/{API_VERSION}/{endpoint.lstrip('/')}"
	if query:
		url = f"{url}?{query}"
	try:
		return make_get_request(url, headers=_headers(account))
	except Exception as e:
		return _failure(e)


def get_call_records(account, from_date=None, to_date=None, page=1, limit=100, **filters) -> dict:
	"""GET /v1/call/records — the Call Detail Records API, and the authoritative pull source.

	The endpoint was `/v1/call-report` here and had never run, because no account carried an API
	token. Against a live token it answers 403 through AWS API Gateway: no such route. The real path
	is `/v1/call/records`, confirmed against account 214181.

	A record names its parties explicitly, which the webhook does not: `client_number` is always the
	customer and `did_number` always ours, whichever way the call went. It also carries `call_id` (the
	same key the webhook sends), `direction`, `status`, `call_duration`, `date` + `time`, `end_stamp`,
	`hangup_cause`, `recording_url`, `aws_call_recording_identifier`, and a `call_flow` whose Agent
	entries carry the agent's email.

	Dates go through verbatim, formatted 'YYYY-MM-DD HH:MM:SS' by the caller. Paginated; filterable by
	`call_id`, `did_number`, `direction`, `call_type`.
	"""
	params = {"from_date": from_date, "to_date": to_date, "page": page, "limit": limit}
	params.update(filters)
	return _get(account, "call/records", params)
