"""Acefone HTTP client + settings helpers.

Adapted from sanskar-onehash/crm_acefone_integration (MIT).

Thin wrapper over Acefone's REST API (https://api.acefone.in, version path
/v1/). Auth is a Bearer token that lives PER TENANT on an `Acefone Account` doc
— this module is account-driven: every HTTP call takes the account whose creds
to use (resolved upstream by acefone/routing.py). The only GLOBAL state is the
`Acefone Settings` kill-switch. We keep this module side-effect free: it never
writes a CRM Call Log — that lives in handler.py. Mirrors the kill-switch +
defensive-POST conventions of tatva_connect/wati/api.py.

Click-to-call returns only {"success": bool, "message": str} (no synchronous
call id), so correlation back to a CRM Call Log row is carried via
`custom_identifier` (echoed in the webhook) — see handler.py.
"""
import json
import re
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import make_get_request, make_post_request

API_VERSION = "v1"
SETTINGS = "CRM Acefone Settings"
DEFAULT_BASE_URL = "https://api.acefone.in"


def is_enabled() -> bool:
	"""Acefone master kill-switch (GLOBAL). Defaults to enabled if never saved.

	Fresh DB read (not get_cached_doc) so flipping the switch takes effect
	immediately across all worker processes. Per-account `enabled` flags are
	checked by the routing/handler layer, not here.
	"""
	val = frappe.db.get_single_value(SETTINGS, "enabled")
	return True if val is None else bool(val)


def assert_enabled():
	"""Block outbound calls when the GLOBAL kill-switch is off."""
	if not is_enabled():
		frappe.throw(
			_("Acefone is disabled (CRM Acefone Settings → Enabled is off)."),
			title=_("Acefone disabled"),
		)


def normalize_number(number) -> str:
	"""Canonicalise a phone number to bare digits (strip +, -, spaces, etc.)."""
	return re.sub(r"\D", "", str(number or ""))


def base_url_of(account) -> str:
	"""This account's base URL with any trailing slash stripped (or the default)."""
	base = (account.get("base_url") or DEFAULT_BASE_URL).strip()
	return base.rstrip("/")


def _headers(account) -> dict:
	"""Bearer auth from this Acefone Account's api_token Password field."""
	token = account.get_password("api_token")
	return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _post(account, endpoint: str, body: dict) -> dict:
	"""POST to Acefone (per-account) and ALWAYS return a parsed body.

	make_post_request raises on a 4xx/5xx; we catch it and surface Acefone's
	error body (or a synthetic one) so callers can show a clean message instead
	of a 500. Mirrors wati.api._post.
	"""
	url = f"{base_url_of(account)}/{API_VERSION}/{endpoint.lstrip('/')}"
	try:
		return make_post_request(url, headers=_headers(account), data=json.dumps(body))
	except Exception as e:
		resp = getattr(frappe.flags, "integration_request", None)
		if resp is not None:
			try:
				return resp.json()
			except Exception:
				return {"success": False, "message": (getattr(resp, "text", "") or str(e))[:400]}
		return {"success": False, "message": str(e)[:400]}


def click_to_call(account, destination_number, agent_number, caller_id=None, custom_identifier=None) -> dict:
	"""POST /v1/click_to_call on `account` — bridge an agent's phone to the destination.

	Returns Acefone's body, e.g. {"success": True, "message": "..."}. There is
	NO synchronous call id; `custom_identifier` (we pass the CRM Call Log name)
	is echoed back in the CDR webhook for deterministic correlation.
	"""
	# Acefone wants BARE DIGITS — a leading "+" is rejected ("Unable to process this
	# request"). Normalize all numbers here (the single choke point).
	body = {
		"agent_number": normalize_number(agent_number),
		"destination_number": normalize_number(destination_number),
		"async": "1",
	}
	if caller_id:
		body["caller_id"] = normalize_number(caller_id)
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
		resp = getattr(frappe.flags, "integration_request", None)
		if resp is not None:
			try:
				return resp.json()
			except Exception:
				return {"success": False, "message": (getattr(resp, "text", "") or str(e))[:400]}
		return {"success": False, "message": str(e)[:400]}


def get_call_report(account, from_date=None, to_date=None, page=1, limit=100, **filters) -> dict:
	"""GET /v1/call-report on `account` — the authoritative PULL source for calls.

	Each record carries `recording_file_link` (the recording URL) and `unique_id`
	(the call's unique id), plus `call_hint` (direction), `source`/`destination`,
	`call_answered_by`, `status`, and timestamps. Filterable by from_date/to_date,
	`call_id`, `did`, `dest_num`, `call_status`; paginated (`page`, `limit`).

	Dates are passed through verbatim — the caller formats them to Acefone's
	expected 'YYYY-MM-DD HH:MM:SS'. Returns Acefone's parsed body (shape pinned on
	the first live capture — see reconcile.py).
	"""
	params = {"from_date": from_date, "to_date": to_date, "page": page, "limit": limit}
	params.update(filters)
	return _get(account, "call-report", params)
