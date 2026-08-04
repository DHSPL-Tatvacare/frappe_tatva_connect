"""WATI HTTP client — the wire, and nothing above it.

Thin wrapper over WATI's REST API, live-verified shapes (see vault design 05-integrations/
01-wati-whatsapp.md §9). A WATI tenant maps to one `WhatsApp Account` row: base URL in `url`
(e.g. https://live-mt-server.wati.io/360078), JWT in the `token` Password field.

TWO versions, and they do not share a base URL. Every SEND is v1 and tenant-scoped; every READ is v3
and host-rooted. `base_url(account, version)` is the one function that knows the difference — build a
URL any other way and WATI answers 404.

Nothing here decides anything. No kill-switch, no vendor gate, no routing — those belong to the
channel and to the adapter above it. This module knows how to talk to WATI and how to hand back what
WATI said.
"""
from urllib.parse import urlparse

import frappe
import requests  # ALLOWLIST 2026-06-29: multipart uploads (files=) and raw-byte downloads — neither of which make_*_request can express. Do NOT convert the calls it guards.
from frappe.integrations.utils import make_get_request
from frappe.utils import get_request_session

from tatva_connect.channels import transfer

# WATI requires this exact content-type (not plain application/json).
CONTENT_TYPE = "application/json-patch+json"

# The two API versions this tenant answers on. They do NOT share a base — see `base_url`.
API_V1 = "v1"
API_V3 = "v3"

# WATI caps a v3 page at 100 items; both page params are required (a missing one is a 400).
MAX_PAGE_SIZE = 100

# Safety cap on a v3 conversation walk, so a runaway thread can never loop for ever.
MAX_PAGES = 50

# A media download is bytes over the wire, not an API call — it gets its own, longer budget.
MEDIA_TIMEOUT = 60

# A send must not hang for ever: the same budget `automation.actions` gives an endpoint that must answer.
SEND_TIMEOUT = 30


def base_url(account, version: str = API_V1) -> str:
	"""The account's API root for one API version — the ONE place that knows the two differ.

	v1 is TENANT-scoped and IS the account's own `url` (`https://host/360078/api/v1/...`). v3 is HOST
	ROOT with no tenant segment (`https://host/api/ext/v3/...`) and answers 404 on the tenant path, so
	the tenant is stripped rather than appended to.

	The trailing slash is dropped here for both. An operator pasting the URL with one built
	`https://host/360078//api/v1/...`, WATI answered 404 to EVERY call, and nothing said why.
	"""
	url = (account.get("url") or "").rstrip("/")
	if version == API_V1:
		return url
	if version != API_V3:
		raise ValueError(f"unknown WATI API version {version!r}; known: {API_V1}, {API_V3}")
	parts = urlparse(url)
	return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else url


def _headers(token: str) -> dict:
	return {"Authorization": f"Bearer {token}", "Content-Type": CONTENT_TYPE}


class OutcomeUnknown(Exception):
	"""The send got no answer. NOT a failure — the message may well have been accepted and delivered.

	Reporting this as failed is what makes a rep click Send twice and a patient receive one clinical
	message twice; reporting it as sent would hide a real non-delivery. It is its own outcome.
	"""


def _post(url: str, token: str, body: dict) -> dict:
	"""POST to WATI and return the parsed body. Raises OutcomeUnknown when there was no answer.

	WATI signals some errors as HTTP 200 + {"result": false} (credits, session) and others as a 4xx
	(e.g. a template sent without its required params). BOTH ARE ANSWERS, so the status is never
	consulted: whatever came back is parsed and handed to `_classify`, the one place a send outcome is
	decided. An unreadable body becomes the same refusal shape `send_session_file` already returns.

	IT MUST TIME OUT, AND `make_post_request` CANNOT. `make_request` (frappe `integrations/utils.py:48`)
	declares no `timeout` and no `**kwargs`, so passing one is a `TypeError`, not a no-op; and there is
	no door behind it either — `get_request_session` takes only `max_retries`, and `requests.Session`
	holds no default timeout. So the session is asked directly, which is exactly what
	`automation.actions._call_api` already does with `_API_TIMEOUT_SECONDS`. Frappe's own session is
	still what sends — its retry adapter and pooling are unchanged — and POST is NOT in urllib3's
	retryable set, so the mounted `Retry(total=5, status_forcelist=[500])` can never replay a send.

	WHAT IS GIVEN UP, deliberately: `make_request` also stamps `frappe.flags.integration_request` and
	`log_error()`s on any exception. Nothing reads that flag for WATI, and stamping it is the hazard
	below, not a feature; the auto-log only ever fired for answers this function treats as ordinary
	refusals, which the caller surfaces anyway.

	A timeout or a connection error is not an answer, and it carries no response to read. The reply
	must come from THIS call's exception — `frappe.flags.integration_request` is assigned only once a
	response exists and is never cleared, so reading it here returned the PREVIOUS send's body: an
	unsent message was stamped accepted, carrying another message's correlation id.
	"""
	try:
		response = get_request_session().request(
			"POST", url, headers=_headers(token), json=body, timeout=SEND_TIMEOUT,
		)
	except Exception as e:
		raise OutcomeUnknown(str(e)[:400]) from e
	try:
		return response.json()
	except ValueError:
		return {"result": False, "info": (response.text or "")[:400]}


def send_template_message(account, to_number: str, template_name: str, broadcast_name: str, parameters=None):
	"""POST /api/v1/sendTemplateMessage (singular — returns local_message_id).

	`parameters` is a list of {"name": str, "value": str} per WATI's template placeholder contract;
	pass [] for a static-body template.
	"""
	token = account.get_password("token")
	url = f"{base_url(account)}/api/v1/sendTemplateMessage?whatsappNumber={to_number}"
	body = {"template_name": template_name, "broadcast_name": broadcast_name, "parameters": parameters or []}
	return _post(url, token, body)


def send_session_message(account, to_number: str, message: str):
	"""POST /api/v1/sendSessionMessage/{number} — free-text within an open 24h session."""
	token = account.get_password("token")
	url = (
		f"{base_url(account)}/api/v1/sendSessionMessage/{to_number}"
		f"?messageText={frappe.utils.quote(message or '')}"
	)
	return _post(url, token, {})


def send_session_file(account, to_number: str, filename: str, content: bytes, mimetype: str, caption: str = ""):
	"""POST /api/v1/sendSessionFile/{number}?caption= — multipart upload (field 'file').

	Same outcome rule as `_post`: a response is an answer whatever it says, and no response is
	OutcomeUnknown. An upload that timed out may still have reached the patient.
	"""
	token = account.get_password("token")
	url = f"{base_url(account)}/api/v1/sendSessionFile/{to_number}"
	params = {"caption": caption} if caption else {}
	# Multipart: do NOT set Content-Type (requests sets the boundary).
	try:
		resp = requests.post(
			url,
			headers={"Authorization": f"Bearer {token}"},
			params=params,
			files={"file": (filename, content, mimetype or "application/octet-stream")},
			timeout=MEDIA_TIMEOUT,
		)
	except Exception as e:
		raise OutcomeUnknown(str(e)[:400]) from e
	try:
		return resp.json()
	except ValueError:
		return {"result": False, "info": (resp.text or "")[:400]}


def send_session_file_via_url(account, to_number: str, file_url: str, caption: str = ""):
	"""POST /api/v1/sendSessionFileViaUrl/{number}?fileUrl=&caption= — for http(s) files."""
	token = account.get_password("token")
	url = (
		f"{base_url(account)}/api/v1/sendSessionFileViaUrl/{to_number}"
		f"?fileUrl={frappe.utils.quote(file_url)}&caption={frappe.utils.quote(caption or '')}"
	)
	return _post(url, token, {})


def fetch_conversation_messages(account, target, page_number: int = 1, page_size: int = MAX_PAGE_SIZE):
	"""GET /api/ext/v3/conversations/{target}/messages — the ONE read: recover, backfill and refresh.

	`target` is any identity WATI accepts for the thread — conversation id, phone, contact id or bsuid;
	a contact has exactly one conversation, so all four name the same item set. Both query params are
	REQUIRED and `page_size` is capped at 100. Returns the raw `message_list` (v3 snake_case) — the
	adapter, not the wire, does the translating.
	"""
	token = account.get_password("token")
	url = (
		f"{base_url(account, API_V3)}/api/ext/v3/conversations/{frappe.utils.quote(str(target))}/messages"
		f"?page_number={int(page_number)}&page_size={min(int(page_size), MAX_PAGE_SIZE)}"
	)
	return (make_get_request(url, headers=_headers(token)) or {}).get("message_list") or []


def iter_conversation_messages(account, target, page_size: int = MAX_PAGE_SIZE, max_pages: int = MAX_PAGES):
	"""Walk a conversation page by page, yielding each message, so a caller looking for ONE can stop.

	Bounded twice over: a short page ends the walk, and `max_pages` ends it regardless. Raises on an API
	error, so a caller reconciling from this never proceeds on a failed fetch.
	"""
	for page in range(1, max_pages + 1):
		items = fetch_conversation_messages(account, target, page_number=page, page_size=page_size)
		yield from items
		if len(items) < page_size:
			return
	frappe.log_error(
		title="WATI v3 conversation read hit its page cap",
		message=f"account={getattr(account, 'name', '')} target={target} pages={max_pages}",
	)


def fetch_message_media(account, message_id):
	"""GET /api/ext/v3/conversations/messages/file/{id} -> (content, content_type, filename), or None.

	Raw bytes, not JSON — verified live at 3.5 MB of application/pdf with the filename in
	Content-Disposition. A 404 means WATI holds no file for this message, which is an answer rather than
	a failure, so it returns None and lets the caller fall back.
	"""

	token = account.get_password("token")
	url = (
		f"{base_url(account, API_V3)}/api/ext/v3/conversations/messages/file/"
		f"{frappe.utils.quote(str(message_id))}"
	)
	resp = requests.get(
		url,
		headers={"Authorization": f"Bearer {token}"},
		timeout=MEDIA_TIMEOUT,
		stream=True,
		allow_redirects=False,  # This URL is BUILT from the account's own base_url, so a 3xx off it is unexpected rather than the delivery — unlike `get_media`, whose URL a payload names and whose hop is followed and vetted.
	)
	if resp.status_code == 404:
		return None
	resp.raise_for_status()
	return (
		transfer.read_capped(resp),
		resp.headers.get("content-type") or "application/octet-stream",
		_filename_from_disposition(resp.headers.get("content-disposition")),
	)


def _filename_from_disposition(header):
	"""The filename WATI names in Content-Disposition, parsed by werkzeug's own header parser."""
	if not header:
		return None
	from werkzeug.http import parse_options_header

	options = parse_options_header(header)[1]
	return options.get("filename") or options.get("filename*")


def get_message_templates(account, page_size: int = 500, page_number: int = 1):
	"""GET /api/v1/getMessageTemplates — the tenant's templates (already approved on WATI)."""
	token = account.get_password("token")
	url = f"{base_url(account)}/api/v1/getMessageTemplates?pageSize={page_size}&pageNumber={page_number}"
	return make_get_request(url, headers=_headers(token))


def get_media(account, data: str) -> tuple[bytes, str]:
	"""Download media by the URL a LIVE WEBHOOK named — a full showFile URL, or the relative
	'data/images/<uuid>.jpg' form. Returns (content_bytes, content_type). Requires the account Bearer
	token — an unauthenticated GET returns 401 (verified).

	The webhook's own route, kept because the webhook payload is where that URL comes from. A message
	pulled from HISTORY is read by id instead, through `fetch_message_media`.
	"""

	url = data if data.startswith("http") else f"{base_url(account)}/api/file/showFile?fileName={data}"
	# SSRF guard: the media URL can originate from a webhook payload. Restrict it to this account's operator-configured host allowlist (blank = any public host; the IP block still runs).
	allowed = [row.host for row in (account.get("custom_media_host_allowlist") or [])]
	# A provider serves media off a CDN as often as off its own host, so the hop is FOLLOWED and every
	# destination is vetted — the allowlist and the private-IP block apply to each one, not only the first.
	content, content_type = transfer.fetch_capped(
		url,
		timeout=MEDIA_TIMEOUT,
		headers={"Authorization": f"Bearer {account.get_password('token')}"},
		allowed_hosts=allowed,
	)
	return content, content_type or "application/octet-stream"
