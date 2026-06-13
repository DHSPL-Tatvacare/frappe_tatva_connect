"""WATI HTTP client.

Thin wrapper over WATI's REST API, live-verified shapes (see vault design
05-integrations/01-wati-whatsapp.md §9). A WATI tenant maps to one
`WhatsApp Account` row: base URL in `url` (e.g. https://live-mt-server.wati.io/360078),
JWT in the `token` Password field. We detect WATI accounts by the host marker.
"""
import json
import re
from typing import NamedTuple

import frappe
from frappe import _
from frappe.integrations.utils import make_post_request, make_get_request

# WATI multi-tenant server host; present in every tenant's base URL.
WATI_HOST_MARKER = "wati.io"

# WATI requires this exact content-type (not plain application/json).
CONTENT_TYPE = "application/json-patch+json"


def is_wati_account(account) -> bool:
	"""True if the WhatsApp Account is a WATI tenant (vs Meta Cloud).

	Prefer the explicit `custom_is_wati` flag; fall back to the URL host marker
	so detection works before the custom field exists.
	"""
	if account.get("custom_is_wati"):
		return True
	return WATI_HOST_MARKER in (account.get("url") or "")


def assert_wati(account):
	"""Fail loud if asked to send through a non-WATI account.

	This is guardrail #3 of the no-Meta guarantee: a misconfigured account
	raises a visible error instead of silently falling back to Meta.
	"""
	if not is_wati_account(account):
		frappe.throw(
			_("WATI is the only allowed transport. WhatsApp Account '{0}' is not a WATI account — refusing to send.").format(
				getattr(account, "name", account)
			),
			title=_("Blocked non-WATI send"),
		)


def is_enabled() -> bool:
	"""WATI master kill-switch. Dormant by default — OFF until explicitly enabled
	(a blank/unsaved single reads as disabled)."""
	return bool(frappe.db.get_single_value("CRM WATI Settings", "enabled"))


def assert_enabled():
	"""Block sends when the kill-switch is off (the switch must stop send AND receive)."""
	if not is_enabled():
		frappe.throw(
			_("WATI is disabled (WATI Settings → Enabled is off). No messages are sent."),
			title=_("WATI disabled"),
		)


def normalize_number(number: str) -> str:
	"""Canonicalise a phone number to bare E.164 digits (strip +, -, spaces).

	Stock frappe_whatsapp.format_number only strips a leading '+', leaving
	hyphens like '+91-7753022190' that WATI rejects. We always use this.
	"""
	return re.sub(r"\D", "", number or "")


def _headers(token: str) -> dict:
	return {"Authorization": f"Bearer {token}", "Content-Type": CONTENT_TYPE}


def _post(url: str, token: str, body: dict) -> dict:
	"""POST to WATI and ALWAYS return the parsed body (never raise on non-2xx).

	WATI signals some errors as HTTP 200 + {"result": false} (credits, session)
	and others as a 4xx (e.g. a template sent without its required params).
	make_post_request raises on the 4xx; we catch it and return WATI's error
	body so the caller surfaces a clean message instead of a 500.
	"""
	try:
		return make_post_request(url, headers=_headers(token), data=json.dumps(body))
	except Exception as e:
		resp = getattr(frappe.flags, "integration_request", None)
		if resp is not None:
			try:
				return resp.json()
			except Exception:
				return {"result": False, "info": (getattr(resp, "text", "") or str(e))[:400]}
		return {"result": False, "info": str(e)[:400]}


class WatiSendResult(NamedTuple):
	failed: bool
	message_id: str | None
	reason: str | None  # human-readable detail on failure (None on success)


def classify_send_response(resp) -> WatiSendResult:
	"""Single source of truth for 'did this WATI send succeed?' — used by BOTH the
	manual-send path (message._wati_apply_response) and the automated-notification path
	(notification.notify), so the contract is encoded once, never two ways.

	WATI is inconsistent across endpoints: template send returns {"result": true};
	session-file send returns {"result": "<id-string>"} (no `ok` key); some session
	endpoints add {"ok": true}. Errors come back as {"result": false}/{"ok": false}
	(HTTP 200) or as a body our _post normalised to {"result": false, "info": ...} on a
	4xx/timeout. Rule: an explicit false flag, a non-dict, or an empty/falsy `result`
	with no truthy `ok` is a failure; anything else succeeded.

	Pure — no side effects. Callers apply their own (throw / raise / insert a row)."""
	if not isinstance(resp, dict):
		return WatiSendResult(True, None, str(resp)[:400])

	ok = resp.get("ok")
	result = resp.get("result")
	failed = (
		ok is False
		or result is False
		or (ok is not True and result in (None, "", "false", "False", 0))
	)
	if failed:
		# WATI carries the human reason in message.failedDetail; our _post normalises
		# 4xx/timeout errors to resp["info"]. Prefer whichever is present (never the raw payload).
		msg = resp.get("message")
		reason = (
			resp.get("info")
			or (msg.get("failedDetail") if isinstance(msg, dict) else None)
			or (msg if isinstance(msg, str) else None)
		)
		return WatiSendResult(True, None, reason)

	msg = resp.get("message") if isinstance(resp.get("message"), dict) else {}
	message_id = (
		resp.get("local_message_id")
		or msg.get("localMessageId")
		or msg.get("whatsappMessageId")
		# file sends return the id as the `result` string itself.
		or (result if isinstance(result, str) and result not in ("true", "false") else None)
	)
	return WatiSendResult(False, message_id, None)


def template_param_names(template) -> list:
	"""Ordered WATI param names for {{1}},{{2}},… — the keys of the template's sample_values
	(WATI customParams in body order). One source of truth for BOTH the manual and the
	automated-notification send paths. [] if none; callers fall back to the positional index."""
	try:
		sv = json.loads(template.sample_values) if template.sample_values else {}
		return list(sv.keys())
	except Exception:
		return []


def send_template_message(account, to_number: str, template_name: str, broadcast_name: str, parameters=None):
	"""POST /api/v1/sendTemplateMessage (singular — returns local_message_id).

	`parameters` is a list of {"name": str, "value": str} per WATI's template
	placeholder contract; pass [] for a static-body template.
	"""
	token = account.get_password("token")
	url = f"{account.url}/api/v1/sendTemplateMessage?whatsappNumber={to_number}"
	body = {
		"template_name": template_name,
		"broadcast_name": broadcast_name,
		"parameters": parameters or [],
	}
	return _post(url, token, body)


def send_session_message(account, to_number: str, message: str):
	"""POST /api/v1/sendSessionMessage/{number} — free-text within an open 24h session."""
	token = account.get_password("token")
	url = f"{account.url}/api/v1/sendSessionMessage/{to_number}?messageText={frappe.utils.quote(message or '')}"
	return _post(url, token, {})


def send_session_file(account, to_number: str, filename: str, content: bytes, mimetype: str, caption: str = ""):
	"""POST /api/v1/sendSessionFile/{number}?caption= — multipart upload (field 'file')."""
	import requests

	token = account.get_password("token")
	url = f"{account.url}/api/v1/sendSessionFile/{to_number}"
	params = {"caption": caption} if caption else {}
	# Multipart: do NOT set Content-Type (requests sets the boundary).
	try:
		resp = requests.post(
			url,
			headers={"Authorization": f"Bearer {token}"},
			params=params,
			files={"file": (filename, content, mimetype or "application/octet-stream")},
			timeout=60,
		)
		try:
			return resp.json()
		except Exception:
			return {"result": False, "info": (resp.text or "")[:400]}
	except Exception as e:
		return {"result": False, "info": str(e)[:400]}


def send_session_file_via_url(account, to_number: str, file_url: str, caption: str = ""):
	"""POST /api/v1/sendSessionFileViaUrl/{number}?fileUrl=&caption= — for http(s) files."""
	token = account.get_password("token")
	url = (
		f"{account.url}/api/v1/sendSessionFileViaUrl/{to_number}"
		f"?fileUrl={frappe.utils.quote(file_url)}&caption={frappe.utils.quote(caption or '')}"
	)
	return _post(url, token, {})


def get_messages(account, number: str, page_size: int = 50, page_number: int = 1):
	"""GET /api/v1/getMessages/{number} — history (body in finalText). Reconciler/backfill."""
	token = account.get_password("token")
	url = f"{account.url}/api/v1/getMessages/{number}?pageSize={page_size}&pageNumber={page_number}"
	return make_get_request(url, headers=_headers(token))


def get_all_messages(account, number: str, page_size: int = 100, max_pages: int = 50):
	"""Pull a number's FULL message history (all pages) from WATI getMessages.

	Loops pageNumber until a short/empty page (or the safety cap) and returns the
	flat list of message items. Raises on an API error (so a caller reconciling
	from this never proceeds on a failed fetch).
	"""
	items = []
	for page in range(1, max_pages + 1):
		resp = get_messages(account, number, page_size=page_size, page_number=page)
		page_items = ((resp or {}).get("messages") or {}).get("items") or []
		items.extend(page_items)
		if len(page_items) < page_size:
			break
	else:
		frappe.log_error(
			title="WATI getMessages hit page cap",
			message=f"account={getattr(account, 'name', '')} number={number} pages={max_pages}",
		)
	return items


def get_message_templates(account, page_size: int = 500, page_number: int = 1):
	"""GET /api/v1/getMessageTemplates — the tenant's templates (already approved on WATI)."""
	token = account.get_password("token")
	url = f"{account.url}/api/v1/getMessageTemplates?pageSize={page_size}&pageNumber={page_number}"
	return make_get_request(url, headers=_headers(token))


def get_media(account, data: str) -> tuple[bytes, str]:
	"""Download a WATI inbound media file. `data` is either a full showFile URL
	(live webhook) or a relative path like 'data/images/<uuid>.jpg' (getMessages
	history). Returns (content_bytes, content_type). Requires the account Bearer
	token — an unauthenticated GET returns 401 (verified)."""
	import requests

	url = data if data.startswith("http") else f"{account.url}/api/file/showFile?fileName={data}"
	token = account.get_password("token")
	resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
	resp.raise_for_status()
	return resp.content, resp.headers.get("content-type") or "application/octet-stream"
