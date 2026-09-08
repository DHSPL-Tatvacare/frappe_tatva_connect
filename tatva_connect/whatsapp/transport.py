"""WATI HTTP client — the wire, and nothing above it.

Thin wrapper over WATI's REST API, live-verified shapes (see vault design 05-integrations/
01-wati-whatsapp.md §9). One `WhatsApp Account` row = one WhatsApp NUMBER: base URL in `url`
(e.g. https://live-mt-server.wati.io/360078), JWT in the `token` Password field. A tenant with several
numbers is several rows sharing that url and token — see the multi-number paragraph below.

TWO versions, and they do not share a base URL. Every SEND is v1 and tenant-scoped; every READ is v3
and host-rooted. `base_url(account, version)` is the one function that knows the difference — build a
URL any other way and WATI answers 404.

ONE ACCOUNT CAN CARRY MANY NUMBERS (WATI allows 25 behind one URL and token). A send that names none
leaves from the account's DEFAULT number, so every brand on a shared account would reach the patient
as the default one. Each send therefore takes `channel_number` — the number it must leave FROM. Blank
means "say nothing", which is byte-for-byte the request a single-number account always sent. WHICH
number, and whether the account has more than one, is the adapter's decision (`wati._channel_number`).

WHERE THAT PARAMETER GOES WAS MEASURED, NOT READ. WATI's multi-number article puts it in the payload
for both endpoints and is wrong about both: in the session body it is accepted, answered `ok: true`,
and ignored. Sending one message four ways to one recipient — body and query, under each of WATI's two
spellings — showed only the QUERY spelling opening a second conversation. The v1 template endpoint
honoured none of the four, so `send_template_message` speaks v3 instead, whose `channel` field genuinely
works. Which API version a call speaks is this module's business and nobody else's.

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


def _headers(token: str, content_type: str = CONTENT_TYPE) -> dict:
	"""v1 demands `application/json-patch+json`; v3 takes plain json. The default is v1's, so every
	existing caller is unchanged."""
	return {"Authorization": f"Bearer {token}", "Content-Type": content_type}


class OutcomeUnknown(Exception):
	"""The send got no answer. NOT a failure — the message may well have been accepted and delivered.

	Reporting this as failed is what makes a rep click Send twice and a patient receive one clinical
	message twice; reporting it as sent would hide a real non-delivery. It is its own outcome.
	"""


def _post(url: str, token: str, body: dict, content_type: str = CONTENT_TYPE) -> dict:
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
			"POST", url, headers=_headers(token, content_type), json=body, timeout=SEND_TIMEOUT,
		)
	except Exception as e:
		raise OutcomeUnknown(str(e)[:400]) from e
	try:
		return response.json()
	except ValueError:
		return {"result": False, "info": (response.text or "")[:400]}


def send_template_message(
	account, to_number: str, template_name: str, broadcast_name: str, parameters=None,
	channel_number: str = "", local_message_id: str = "",
):
	"""POST /api/ext/v3/messageTemplates/send — THE template send.

	v3 because v1 cannot name the number a template leaves from. `/api/v1/sendTemplateMessage` was given
	both of WATI's documented spellings, in the body AND in the query, and all four arrived from the
	account's default number while the API answered success — so on an account with several numbers every
	brand would reach the patient as the default brand. `channel` here is the field that actually works;
	omitted, the send leaves from the account's default, which is the only number a single-number account
	has. One call serves both, so there is no second path to keep in step.

	Host-rooted like every v3 call, and plain `application/json`: v1's `json-patch+json` does not apply.

	THE CORRELATION ID IS THE CALLER'S. v1 minted it and handed it back; v3 takes `local_message_id` and
	echoes it per recipient. `wati._classify` reads the ECHO and never the value we sent, so a recipient
	WATI did not accept cannot be recorded under an id nothing will ever confirm.

	One recipient per call. The endpoint accepts up to 10,000, but that is a broadcast rather than the
	message-to-one-patient every send path here is built on — one row and one outcome each.
	"""
	token = account.get_password("token")
	url = f"{base_url(account, API_V3)}/api/ext/v3/messageTemplates/send"
	recipient = {"phone_number": to_number, "custom_params": parameters or []}
	if local_message_id:
		recipient["local_message_id"] = local_message_id
	body = {"template_name": template_name, "broadcast_name": broadcast_name, "recipients": [recipient]}
	if channel_number:
		body["channel"] = channel_number
	return _post(url, token, body, content_type="application/json")


def conversation_target(contact: str, channel_number: str = "") -> str:
	"""How every v3 CONVERSATION call names "this number's thread with this contact".

	WATI scopes a conversation through the target itself — `<channel>:<contact>` — and not through a
	separate field, which is the one place the template endpoint differs (it takes `channel`). Named, the
	call reaches that number's thread; unnamed, it resolves to the CONTACT, and a contact is shared by
	every number on the account. Measured on one contact: the bare number answered with 41 messages from
	both numbers, `919974306678:<contact>` with the 16 that were its own.

	So this is not decoration on a multi-number account — it is the difference between a patient's thread
	and somebody else's. Blank channel keeps the bare contact, which is what a single-number account has
	always sent and the only thing it can mean there.
	"""
	return f"{channel_number}:{contact}" if channel_number else str(contact)


def send_session_message(account, to_number: str, message: str, channel_number: str = ""):
	"""POST /api/ext/v3/conversations/messages/text — free text inside an open 24h session."""
	token = account.get_password("token")
	url = f"{base_url(account, API_V3)}/api/ext/v3/conversations/messages/text"
	body = {"target": conversation_target(to_number, channel_number), "text": message or ""}
	return _post(url, token, body, content_type="application/json")


def send_session_file(
	account, to_number: str, filename: str, content: bytes, mimetype: str, caption: str = "",
	channel_number: str = "",
):
	"""POST /api/ext/v3/conversations/messages/file — multipart upload (field 'file').

	Same outcome rule as `_post`: a response is an answer whatever it says, and no response is
	OutcomeUnknown. An upload that timed out may still have reached the patient.

	Multipart, so `target` and `caption` ride as form fields beside the bytes rather than as JSON.
	"""
	token = account.get_password("token")
	url = f"{base_url(account, API_V3)}/api/ext/v3/conversations/messages/file"
	data = {"target": conversation_target(to_number, channel_number)}
	if caption:
		data["caption"] = caption
	# Multipart: do NOT set Content-Type (requests sets the boundary).
	try:
		resp = requests.post(
			url,
			headers={"Authorization": f"Bearer {token}"},
			data=data,
			files={"file": (filename, content, mimetype or "application/octet-stream")},
			timeout=MEDIA_TIMEOUT,
		)
	except Exception as e:
		raise OutcomeUnknown(str(e)[:400]) from e
	try:
		return resp.json()
	except ValueError:
		return {"result": False, "info": (resp.text or "")[:400]}


def send_session_file_via_url(account, to_number: str, file_url: str, caption: str = "", channel_number: str = ""):
	"""POST /api/ext/v3/conversations/messages/fileViaUrl — for a file the provider fetches itself."""
	token = account.get_password("token")
	url = f"{base_url(account, API_V3)}/api/ext/v3/conversations/messages/fileViaUrl"
	body = {"target": conversation_target(to_number, channel_number), "file_url": file_url}
	if caption:
		body["caption"] = caption
	return _post(url, token, body, content_type="application/json")


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
	if resp.status_code == 404 or _media_is_gone(resp):
		return None
	resp.raise_for_status()
	return (
		transfer.read_capped(resp),
		resp.headers.get("content-type") or "application/octet-stream",
		_filename_from_disposition(resp.headers.get("content-disposition")),
	)


# WATI's own code for "this message has no file any more" — an ANSWER, not a failure, and it arrives as
# a 400 rather than the 404 the same fact gets on other routes.
MEDIA_GONE = 5004


def _media_is_gone(resp) -> bool:
	"""Did the provider say the media is no longer there, rather than that we asked wrongly?

	It answers 400 for BOTH, and the two could not matter more differently: media expires off a provider
	after some months, so a backfill walking a year of history meets it constantly and nothing is wrong —
	the message still files, captioned "Media unavailable". A malformed id is OURS, and staying loud is
	the only way that is ever noticed. Measured on a real tenant: `{"code":5004,"message":"Message Not
	Found"}` for an expired June attachment, `{"code":400,"message":"Message ID is invalid"}` for a wamid
	handed to an endpoint that wants the provider's own id.

	Read from the BODY's own code, never the status, because the status cannot tell them apart. An
	unreadable body is not a "gone" — it falls through and raises, as anything unrecognised should.
	"""
	if resp.status_code != 400:
		return False
	try:
		return (resp.json() or {}).get("code") == MEDIA_GONE
	except ValueError:
		return False


def _filename_from_disposition(header):
	"""The filename WATI names in Content-Disposition, parsed by werkzeug's own header parser."""
	if not header:
		return None
	from werkzeug.http import parse_options_header

	options = parse_options_header(header)[1]
	return options.get("filename") or options.get("filename*")


def get_message_templates(account, channel_number: str = "", page_size: int = MAX_PAGE_SIZE):
	"""GET /api/ext/v3/messageTemplates — every approved template this account may send, all pages.

	`channel` names whose catalogue to read. A provider account carrying several numbers submits a
	template to all of them, so the lists agree today; asking as the number we will SEND as is still what
	makes the picker's contents and the send's own permission the same question, rather than two that
	happen to match.

	Paged, because v3 caps a page at 100 where v1 took a single 500 — a tenant with more than a hundred
	templates silently showed the picker its first hundred. `total` is what the walk is bounded by, and
	the page cap is the same safety net every other walk here has.
	"""
	token = account.get_password("token")
	templates, page = [], 1
	while page <= MAX_PAGES:
		url = (
			f"{base_url(account, API_V3)}/api/ext/v3/messageTemplates"
			f"?page_number={page}&page_size={min(int(page_size), MAX_PAGE_SIZE)}"
		)
		if channel_number:
			url += f"&channel={frappe.utils.quote(channel_number)}"
		body = make_get_request(url, headers=_headers(token, "application/json")) or {}
		batch = body.get("templates") or []
		templates.extend(batch)
		if len(batch) < min(int(page_size), MAX_PAGE_SIZE) or len(templates) >= (body.get("total") or 0):
			break
		page += 1
	return templates


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
