"""WATI — the first provider on the WhatsApp channel.

It DECLARES what it is and what it can truthfully do, and it IMPLEMENTS one job: turning WATI's
payloads into `ChannelEvent`s and turning our send calls into WATI HTTP calls. Everything else —
which leads a message belongs to, what gets written, when a switch is off — belongs to the channel and
is not restated here.

WATI carries no vendor sub-event, so the spine's `event` is always None: the event family comes from
the payload's own `eventType`.

Two identities, and confusing them has cost real messages:
  * `localMessageId` is the CORRELATION id. It is the one WATI echoes on every status event, so it is
    the only thing worth storing when we send — a `whatsappMessageId` stored instead is an id no status
    event ever mentions, and that message's status never updates again.
  * `id` is WATI's own internal message id. It is the ONE identity present on BOTH the live webhook and
    the v3 history, so it is what live ingest, backfill and recovery all de-duplicate on.

Three dialects, TWO normalizers, ONE envelope. The webhook and v1 speak camelCase; the v3 reads speak
snake_case (`local_message_id`, `conversation_id`). `normalize` reads a webhook payload and
`normalize_history` reads a v3 item, and both build the same `ChannelEvent` — nothing downstream can
tell which produced it, and no WATI field name gets past this module.

A status event carries `conversationId` and `id` on 100% of live traffic; `waId` and `bsuid` are absent
from every one of them. That is why recovery keys on the conversation and never on a phone number.
"""
import frappe

from tatva_connect.channels import contract
from tatva_connect.channels import event as channel_event
from tatva_connect.whatsapp import channel, ingest, recovery, routing, transport

DECLARATION = contract.declare(
	channel="whatsapp",
	provider="WATI",
	account_doctype="WhatsApp Account",
	# Every one of these is carried by a real WATI event on live traffic: sentMessageDELIVERED_v2, sentMessageREAD_v2, sentMessageREPLIED_v2, templateMessageFailed, the *Sent_v2 pair, and the interactiveButtonReply on an inbound tap.
	outcomes={"sent", "delivered", "read", "replied", "clicked", "failed"},
	# Neither `lists` nor `buttons`: both arrive INBOUND on the webhook and both are read (a tap becomes `clicked`, with its id), but neither has a send-side builder on our contract — and a capability we cannot exercise is exactly the lie this field exists to prevent. Reading one is not offering it. `recover_message`/`recover_media` are the v3 reads: WATI can hand back the ONE message a status names, and that message's file by id. A provider that cannot declares neither, and an orphan status is logged and dropped exactly as before.
	capabilities={
		"templates", "media", "session", "backfill", "recover_message", "recover_media",
	},
)

# WATI eventType -> the canonical outcome. Driven by eventType so a missing `statusString` still maps.
STATUS_BY_EVENT = {
	"templateMessageSent_v2": "sent",
	"sentMessageDELIVERED_v2": "delivered",
	"sentMessageREAD_v2": "read",
	# A reply is not a read. Mapping it to "read" painted a patient answering the message with the same blue double-tick as one who merely opened it, and the reply — the only outcome anybody acts on — was indistinguishable from silence.
	"sentMessageREPLIED_v2": "replied",
	"templateMessageFailed": "failed",
}

# Outbound "Sent" events for messages that originated OUTSIDE Frappe — an agent or bot typed them in the WATI portal. WATI emits both a v1 and a v2 of every event; only the v2 carries localMessageId, so we ingest ONLY the v2 (taking both would double-insert). templateMessageSent_v2 doubles as the "sent" status for OUR OWN template sends — the ingest decides per message which case applies.
OUTBOUND_SENT_EVENTS = {"sessionMessageSent_v2", "templateMessageSent_v2"}

# WATI's history (getMessages) reports the live status as a string, not an eventType.
_OUTCOME_BY_STATUS_STRING = {
	"SENT": "sent", "DELIVERED": "delivered", "READ": "read", "REPLIED": "replied", "FAILED": "failed",
}

_FALSY = {False, "false", "False", "0", None, ""}


def _falsy(v):
	return v in _FALSY


def _outcome_from_status_string(status_string):
	return _OUTCOME_BY_STATUS_STRING.get((status_string or "").upper())


def _leads_for(digits: str, account=None):
	"""The leads this number reaches on THIS account — asked of `whatsapp.routing`, the one brain.

	Screening used a looser rule of its own (any lead on the number, any account) than the worker then
	applied, so a number whose lead sits on a DIFFERENT account passed the filter and was dropped deep
	in ingest — and the delivery was recorded as processed. One rule, asked once, in both places.
	"""
	if not digits:
		return []
	return routing.leads_for_number_and_account(routing.candidates_for_number("+" + digits), account)


# ---------------------------------------------------------------------------
# normalize — the adapter's one parsing job.
# ---------------------------------------------------------------------------
def normalize(payload, account=None):
	"""One WATI payload -> one ChannelEvent, or None when it is not ours to ingest."""
	ev = payload.get("eventType")
	number = channel.normalize_number(payload.get("waId"))
	common = {
		"channel": DECLARATION.channel,
		"provider": DECLARATION.provider,
		"account": account,
		"conversation_id": payload.get("conversationId"),
		"at": payload.get("timestamp") or payload.get("created"),
		"raw": payload,
	}

	if ev == "message" and _falsy(payload.get("owner")):
		button = payload.get("interactiveButtonReply") or {}
		media_type = payload.get("type") or "text"
		return channel_event.build(
			kind="inbound",
			# A tap is a click. A plain text reply is a message, not an outcome, so it carries none — naming one anyway would invent a signal nothing measured.
			outcome="clicked" if button else None,
			wamid=payload.get("whatsappMessageId"),
			provider_message_id=payload.get("id"),
			subject_number=number,
			text=payload.get("text"),
			media_url=payload.get("data"),
			media_type=media_type,
			# WATI puts the ORIGINAL filename in `text` for a document; for an image `text` is a caption.
			filename=payload.get("text") if media_type == "document" else None,
			button_id=button.get("id"),
			button_title=button.get("title"),
			reply_to=payload.get("replyContextId") or None,
			**common,
		)

	if ev in OUTBOUND_SENT_EVENTS:
		return channel_event.build(
			kind="outbound_echo",
			outcome=STATUS_BY_EVENT.get(ev) or _outcome_from_status_string(payload.get("statusString")),
			correlation_id=payload.get("localMessageId"),
			provider_message_id=payload.get("id"),
			wamid=payload.get("whatsappMessageId"),
			subject_number=number,
			text=payload.get("text"),
			media_url=payload.get("data"),
			media_type=payload.get("type") or "text",
			filename=payload.get("text") if (payload.get("type") == "document") else None,
			**common,
		)

	if payload.get("localMessageId"):
		return channel_event.build(
			kind="status",
			outcome=STATUS_BY_EVENT.get(ev) or _outcome_from_status_string(payload.get("statusString")),
			correlation_id=payload.get("localMessageId"),
			provider_message_id=payload.get("id"),
			wamid=payload.get("whatsappMessageId"),
			subject_number=number or None,
			error_code=payload.get("failedCode"),
			error_detail=payload.get("failedDetail"),
			**common,
		)

	return None


# ---------------------------------------------------------------------------
# Spine contract — duck-typed, called by webhooks.spine.
# ---------------------------------------------------------------------------
def screen(payload, event=None, account=None):
	"""(wanted, reason). The membership filter, run inline before anything is enqueued.

	The reason is written onto the declined delivery's log row, so an operator can see why an event was
	not ingested instead of finding a row stuck at Queued with no explanation.
	"""
	ev = payload.get("eventType")
	number = channel.normalize_number(payload.get("waId"))

	if ev == "message" and _falsy(payload.get("owner")):
		if _leads_for(number, account):
			return True, None
		return False, f"no CRM lead holds the number {number or '(none)'}"

	if ev in OUTBOUND_SENT_EVENTS:
		# Either a status confirmation for a row we sent (matched by localMessageId), or a portal/bot message to ingest (matched by a CRM lead on the number). The precise, account-scoped routing happens in the worker; this is only the cheap filter.
		lmid = payload.get("localMessageId")
		if lmid and frappe.db.exists("WhatsApp Message", {"message_id": lmid}):
			return True, None
		if _leads_for(number, account):
			return True, None
		return False, f"neither a message we sent nor a number any CRM lead holds ({number or 'none'})"

	if payload.get("localMessageId"):
		# A status for a row we hold, or an orphan naming a message that never reached us. Both are wanted; `handle` tells them apart. Screening decides — it does not act, or a replay of a declined delivery would re-queue a provider fetch every time it was re-screened.
		return True, None

	return False, f"eventType {ev or '(none)'} is not ingested"


def already_processed(payload, event=None, account=None) -> bool:
	"""Idempotency — WATI redelivers. ONE key: WATI's own `id`, carried by every event and stored by
	every ingest path as `custom_provider_message_id`, scoped to the receiving account.

	A status is never "already processed": writing one is idempotent by construction, and suppressing a
	redelivery is how a delivered/read that arrived twice arrived never.

	There is no second key. A composite of conversation + sender + text read two genuine replies in one
	thread — a patient answering "Yes" on Monday and "Yes" on Thursday — as one message, and silently
	dropped the second. When the id is absent this answers False and `ingest.held_by_lead` decides on
	the same key at the row.
	"""
	ev = normalize(payload, account)
	if not ev or ev.kind == "status" or not ev.provider_message_id:
		return False
	filters = {"custom_provider_message_id": ev.provider_message_id}
	if account:
		filters["whatsapp_account"] = account
	return bool(frappe.db.exists("WhatsApp Message", filters))


def handle(payload, event=None, account=None) -> None:
	"""Normalize, then hand to the channel's persistence. The adapter writes no rows of its own.

	A status naming a message we do not hold is an ORPHAN: the message exists on WATI and never reached
	us. Recovering that one message is the acting half of the decision `screen` declined to make.
	"""
	ev = normalize(payload, account)
	if ev and ev.kind == "status" and not ingest.rows_for_correlation(ev):
		recovery.queue(ev)
		return
	ingest.apply(ev)


def account_for_payload(payload, event=None):
	"""Replay hook: re-derive the receiving account from a STORED payload.

	The live front door resolves the account from the URL token (not a payload field — WATI inbound
	carries no reliable tenant id), so a replayed payload has no token. We fall back to the only
	account-bearing identity in the stored row: an id already pinned to a WhatsApp Message tells us the
	account we sent or ingested it under. No such anchor -> None, and the ingest fails closed.
	"""
	for value in (payload.get("localMessageId"), payload.get("whatsappMessageId")):
		if value:
			account = frappe.db.get_value("WhatsApp Message", {"message_id": value}, "whatsapp_account")
			if account:
				return account
	return None


# ---------------------------------------------------------------------------
# Send surface — what the channel's send paths call.
# ---------------------------------------------------------------------------
def _classify(resp) -> contract.SendResult:
	"""Single source of truth for "did this WATI send succeed?" — used by every send path, so the
	contract is encoded once.

	WATI is inconsistent across endpoints: template send returns {"result": true}; session-file send
	returns {"result": "<id-string>"} (no `ok` key); some session endpoints add {"ok": true}. Errors
	come back as {"result": false}/{"ok": false} (HTTP 200) or as a body `_post` normalised to
	{"result": false, "info": ...} on a 4xx/timeout. Rule: an explicit false flag, a non-dict, or an
	empty/falsy `result` with no truthy `ok` is a failure; anything else succeeded.

	Pure — no side effects. Callers apply their own.
	"""
	if not isinstance(resp, dict):
		return contract.SendResult(False, None, str(resp)[:400])

	ok = resp.get("ok")
	result = resp.get("result")
	failed = (
		ok is False
		or result is False
		or (ok is not True and result in (None, "", "false", "False", 0))
	)
	if failed:
		# WATI carries the human reason in message.failedDetail; `_post` normalises 4xx/timeout errors to resp["info"]. Prefer whichever is present (never the raw payload).
		msg = resp.get("message")
		reason = (
			resp.get("info")
			or (msg.get("failedDetail") if isinstance(msg, dict) else None)
			or (msg if isinstance(msg, str) else None)
		)
		return contract.SendResult(False, None, reason)

	raw_msg = resp.get("message")
	msg = raw_msg if isinstance(raw_msg, dict) else {}
	# ONLY an id WATI's own status events echo. `whatsappMessageId` used to be accepted here as a fallback, and a message stored under one was a message whose delivered/read/failed never arrived: every status event names the localMessageId and nothing else.
	correlation_id = (
		resp.get("local_message_id")
		or msg.get("localMessageId")
		# A file send returns the id as the `result` string itself.
		or (result if isinstance(result, str) and result not in ("true", "false") else None)
	)
	return contract.SendResult(True, correlation_id, None)


def _send(call, *args, **kwargs) -> contract.SendResult:
	"""Run one transport send and classify what came back — the ONE place a send outcome is decided.

	`transport.OutcomeUnknown` means the wire gave no answer. It becomes an `unknown` SendResult, never
	a refusal, so no caller can read a timeout as "did not send" and retry it onto the patient twice.
	"""
	try:
		return _classify(call(*args, **kwargs))
	except transport.OutcomeUnknown as e:
		return contract.SendResult(False, None, str(e), unknown=True)


def _template_doc(template):
	"""Accept a WhatsApp Templates doc or its name."""
	if hasattr(template, "actual_name"):
		return template
	return frappe.get_doc("WhatsApp Templates", template)


def _wire_name(doc) -> str:
	"""The real name the template is registered under on WATI."""
	return doc.actual_name or doc.template_name


def send_template(account, to, template, variables=None, broadcast_name=None) -> contract.SendResult:
	"""Send an approved template. `variables` is the resolved [{name, value}] list; [] for a static body.

	The broadcast label defaults to the clean template name, so WATI groups same-template sends rather
	than scattering them under an account-scoped record id.
	"""
	doc = _template_doc(template)
	wire = _wire_name(doc)
	return _send(
		transport.send_template_message,
		account,
		to_number=channel.normalize_number(to),
		template_name=wire,
		broadcast_name=broadcast_name or f"crm_{frappe.scrub(wire)}",
		parameters=variables or [],
	)


def send_session(account, to, text) -> contract.SendResult:
	"""Free text inside an open 24h session."""
	return _send(transport.send_session_message, account, channel.normalize_number(to), text or "")


def send_media(account, to, filename, content, mimetype, caption="") -> contract.SendResult:
	"""Send bytes we hold. Our own File rows always go this way — the provider cannot authenticate to
	our proxy URL, so a URL send would fail."""
	return _send(
		transport.send_session_file, account, channel.normalize_number(to), filename, content, mimetype, caption
	)


def send_media_url(account, to, file_url, caption="") -> contract.SendResult:
	"""Send a genuine external link — one we never held the bytes for."""
	return _send(transport.send_session_file_via_url, account, channel.normalize_number(to), file_url, caption)


def list_templates(account):
	"""The account's approved templates, as WATI reports them."""
	resp = transport.get_message_templates(account) or {}
	items = resp.get("messageTemplates") or resp.get("templates") or resp.get("data") or []
	return [t for t in items if (t.get("status") or "").upper() == "APPROVED"]


def template_variables(account, template) -> list:
	"""The template's ordered variable NAMES for {{1}},{{2}},…

	WATI matches parameters by name, not by the position shown in the body — sending positional "1",
	"2" makes WATI fill the slots blank. The real names are the keys of the mirrored `sample_values`,
	in body order. [] when the template has none; callers fall back to the positional index.
	"""
	doc = _template_doc(template)
	try:
		return list((frappe.parse_json(doc.sample_values) or {}).keys())
	except Exception:
		frappe.log_error(title="WhatsApp: unreadable template sample_values")
		return []


def fetch_media(account, url) -> bytes:
	"""The bytes behind a LIVE WEBHOOK media event, by the URL the webhook named."""
	content, _content_type = transport.get_media(account, url)
	return content


def fetch_media_by_message_id(account, message_id):
	"""(content, filename) for one message's file, by WATI's own message id — capability `recover_media`.

	The id is the identity present on a webhook AND in history, so this one route serves live ingest,
	backfill and recovery alike, and WATI names the real filename in Content-Disposition. None on any
	failure, WITH a log line, so the caller falls back to the event's media URL rather than losing an
	attachment to an endpoint that happens to be down.
	"""
	try:
		found = transport.fetch_message_media(account, message_id)
	except Exception:
		frappe.log_error(
			title="WATI v3 media read failed",
			message=f"message_id={message_id}\n{frappe.get_traceback()}",
		)
		return None
	return (found[0], found[2]) if found else None


def history(account, target):
	"""The full two-way thread — the `backfill` capability's pull source.

	`target` names the conversation: a phone number and its conversation id return an identical item set
	(measured — a contact has exactly one conversation), so a caller passes whichever it holds.
	"""
	return list(transport.iter_conversation_messages(account, target))


def recover_message(account, conversation_id, provider_message_id):
	"""The ONE message a status names, from its own conversation — capability `recover_message`.

	Surgical by construction: it walks the conversation only until that id appears and returns that
	single item, so nothing here can ever hand a caller a whole thread to flush and refill.
	"""
	if not (conversation_id and provider_message_id):
		return None
	wanted = str(provider_message_id)
	for item in transport.iter_conversation_messages(account, conversation_id):
		if str(item.get("id") or "") == wanted:
			return item
	return None


# The v3 dialect: (webhook name, v3 name), drawn from the MEASURED field union of 100 live items. Only fields the endpoint really sends are here — mapping one it does not would read blank for ever and look like a bug in the data rather than a lie in this table. What v3 does NOT send, and what each absence costs: * no contact identifier of ANY kind (no wa_id/phone/contact_id/bsuid) -> the subject number cannot come from the item. It is passed in by the caller; see `normalize_history`. * no local_message_id -> a recovered message carries no correlation id of its own. It too is passed in, from the status event that triggered the recovery. * no `data` -> there is no media URL. Media is read by message id instead (`recover_media`). * no whatsapp_message_id, no template_id, no reply/button context. `event_type` is deliberately unmapped — see `normalize_history` on why direction comes from `owner`.
_HISTORY_KEYS = (
	("id", "id"),
	("conversationId", "conversation_id"),
	("statusString", "status_string"),
	("operatorName", "operator_name"),
	("failedDetail", "failed_detail"),
	("type", "type"),
	("owner", "owner"),
	("timestamp", "timestamp"),
	("created", "created"),
)


def normalize_history(item, account=None, number=None, correlation_id=None):
	"""One v3 history item -> the SAME ChannelEvent `normalize` builds. Two producers, one envelope.

	History is WATI's other dialect, not another source, and it is a THINNER one. Two things the caller
	must supply because the item genuinely does not carry them:

	  `number`          the subject. A v3 item has no contact identifier at all, so without this the
	                    event attributes to nobody. The caller knows it — the backfill from the lead it
	                    is filling, recovery from the conversation's own existing rows.
	  `correlation_id`  the id the provider's status events echo. A v3 item has no local_message_id, so
	                    a recovered outbound row that stored none could never be ticked by the very
	                    status that recovered it.

	Direction comes from `owner`, never from the item's own event_type — that reads "message" both ways,
	and believing it would file an agent's outbound message as a status and lose it.
	"""
	payload = {}
	for camel, snake in _HISTORY_KEYS:
		payload[camel] = item.get(snake)
	payload["type"] = payload.get("type") or "text"
	# WATI's history renders a template's body into final_text; `text` is the unsubstituted form. Prefer the rendered one, or a recovered template bubble shows {{1}} to the rep.
	payload["text"] = item.get("final_text") or item.get("text")
	payload["waId"] = number
	payload["localMessageId"] = correlation_id
	if payload.get("owner"):  # business -> customer (an agent or bot typed it on the provider side)
		payload["eventType"] = "sessionMessageSent_v2"
	else:  # customer -> business
		payload["eventType"] = "message"
		payload["senderName"] = payload.get("operatorName")
	return normalize(payload, account)
