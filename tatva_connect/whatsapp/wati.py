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

from tatva_connect import phone
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
	# Digits with the country code and NO `+`: every send puts the number straight into a URL (`?whatsappNumber=`, `/sendSessionMessage/{number}`), where a `+` decodes as a space. The country code is required because WATI resolves the dialling plan itself — handed a bare 10-digit number it reads the leading digits as a country code and delivers the patient's message to a different subscriber.
	number_format=contract.E164_PLAIN,
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


def _accepts_strangers(account) -> bool:
	"""Does this account enrol a sender no lead holds yet?

	`screen` refuses an inbound message when no lead holds its number, and that refusal is what makes
	enrolment unreachable: the one case the feature exists for is the one case the door is shut on. The
	answer comes from `enrol.is_enabled` — the SAME gates ingest asks behind the door — so the two can
	never disagree; screening still decides nothing about the lead, it only stops refusing the case.

	No account, no opinion: on replay the account can be unresolved, and `db.get_value` with a name of
	None reads the FIRST row rather than none, which would open the door off an unrelated account's tick.
	"""
	if not account:
		return False
	# Local, as `ingest._enrol` does — enrol reaches back into this tree.
	from tatva_connect.whatsapp import enrol

	return enrol.is_enabled(account)


def _account_number(account) -> str:
	"""The number this account IS, as comparable digits — the ONE reader of that field.

	Takes the document the send path already holds, or the account NAME the spine hands the inbound
	path, so neither side spells the fieldname a second time nor decides for itself what "the same
	number" means. Spelling is not identity: `+919...` and `919...` are one number.
	"""
	if not account:
		return ""
	raw = (
		frappe.get_cached_value("WhatsApp Account", account, "custom_wati_channel_number")
		if isinstance(account, str)
		else account.get("custom_wati_channel_number")
	)
	return phone.match_digits(raw)


def _channel_number(account) -> str:
	"""The number a send must leave FROM on a multi-number tenant, or "" on a single-number one.

	A WATI tenant carries up to 25 WhatsApp numbers behind ONE url and ONE token, and a send that names
	no number leaves from that tenant's DEFAULT number. So on a shared tenant every brand would reach
	the patient as the default brand — silently, with WATI answering success.

	DECLARED, never inferred. `custom_wati_multi_number` is the account's own statement that its tenant
	holds more than one number; unticked, this returns "" and the request is byte-for-byte the one a
	single-number tenant has always sent. Inferring it from siblings sharing a url would have made one
	account's wire depend on another row nobody edited.
	"""
	if not account or not account.get("custom_wati_multi_number"):
		return ""
	# An account with no number of its own names none, which is the default — the wire we send today.
	return _account_number(account)


def _foreign_channel(payload, account) -> str:
	"""The number an inbound event landed on when it is NOT this account's — else "".

	One WATI webhook can be registered against several of a tenant's numbers, and then `channelPhoneNumber`
	is the only thing separating them. Registered against the wrong number, every reply to one brand would
	be filed under another brand's leads. Checked only when BOTH sides name a number, so a tenant that
	sends no such field (every payload in the corpus) is screened exactly as before.

	It VERIFIES and never resolves: the URL token still says which account received the event (A.16), and
	this only refuses a payload that contradicts it. Attributing by the field instead would put inbound
	back on an unverified payload value, which is the door the per-account token exists to close.
	"""
	received = phone.match_digits(payload.get("channelPhoneNumber"))
	if not received or not account:
		return ""
	ours = _account_number(account)
	return received if ours and received != ours else ""


# ---------------------------------------------------------------------------
# normalize — the adapter's one parsing job.
# ---------------------------------------------------------------------------
def normalize(payload, account=None):
	"""One WATI payload -> one ChannelEvent, or None when it is not ours to ingest."""
	ev = payload.get("eventType")
	number = phone.match_digits(payload.get("waId"))
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
	number = phone.match_digits(payload.get("waId"))

	foreign = _foreign_channel(payload, account)
	if foreign:
		return False, f"received on {foreign}, which is not this account's WhatsApp number"

	if ev == "message" and _falsy(payload.get("owner")):
		if _leads_for(number, account) or _accepts_strangers(account):
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
		# The same tenant scope `ingest.rows_for_correlation` uses, and for the same reason: an id is
		# minted per tenant, so a redelivery reaching a SIBLING number's webhook is the same delivery.
		filters["whatsapp_account"] = ["in", channel.id_space(account)]
	return bool(frappe.get_all("WhatsApp Message", filters=filters, limit=1))


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
def _reason(resp) -> str | None:
	"""The human sentence in a refusal, wherever this provider put it — never the raw payload.

	`info` is the shape `_post` and `send_session_file` normalise an unreadable or 4xx body into, so a
	transport-level refusal reads the same as a provider-level one.
	"""
	message = resp.get("message")
	return (
		resp.get("info")
		or (message.get("failed_detail") if isinstance(message, dict) else None)
		or (message if isinstance(message, str) else None)
		or (resp.get("error") if isinstance(resp.get("error"), str) else None)
	)


def _classify_broadcast(resp) -> contract.SendResult:
	"""The template envelope: `{success, broadcast_id, recipients:[{local_message_id, errors}]}`.

	Success is per RECIPIENT, not per request. WATI answers `success: true` for a call it accepted and
	reports a refusal against the one recipient it refused, so reading the request flag alone would
	record a message nobody received as sent.

	The correlation id is the one WATI ECHOED, never the one we sent. They are equal when the send was
	accepted, and reading the echo is what makes that an assertion instead of an assumption.
	"""
	recipients = resp.get("recipients") or []
	first = recipients[0] if recipients else {}
	errors = first.get("errors") or []
	if not resp.get("success") or errors or not recipients:
		reason = "; ".join(str(e) for e in errors) or _reason(resp) or "the provider accepted no recipient"
		return contract.SendResult(False, None, reason)
	return contract.SendResult(True, first.get("local_message_id"), None)


def _classify_conversation(resp) -> contract.SendResult:
	"""The conversation envelope — text, media and media-by-url all answer in it:
	`{message: {local_message_id, id, status, conversation_id, ...}}`.

	Here the provider mints the id and hands it back, so there is nothing for the caller to supply. A
	`failed` status is a refusal even though the call itself returned 200 — an accepted CALL is not an
	accepted MESSAGE, which is the distinction that keeps a rep from reading a rejection as delivery.
	"""
	message = resp.get("message") or {}
	if (message.get("status") or "").casefold() == "failed":
		return contract.SendResult(False, None, _reason(resp) or "the provider reported the send failed")
	return contract.SendResult(
		True, message.get("local_message_id"), None, wamid=message.get("whatsapp_message_id")
	)


def _classify(resp) -> contract.SendResult:
	"""Single source of truth for "did this WATI send succeed?" — every send path asks it, so the
	contract is encoded once.

	TWO ENVELOPES, ONE RESULT, the same shape as the two normalizers above: a broadcast answers per
	recipient and a conversation answers with one message, and nothing downstream can tell which
	endpoint replied. Anything that is neither is a refusal — including the `{result: false, info}` the
	transport normalises an unreadable body into — because a send this function cannot read as accepted
	is a send nobody may record as delivered.
	"""
	if not isinstance(resp, dict):
		return contract.SendResult(False, None, str(resp)[:400])
	if "recipients" in resp:
		return _classify_broadcast(resp)
	message = resp.get("message")
	# snake_case is the tell: this provider's other envelope spells the same id `localMessageId`.
	if isinstance(message, dict) and "local_message_id" in message:
		return _classify_conversation(resp)
	return contract.SendResult(False, None, _reason(resp) or str(resp)[:400])


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

	ONE ENDPOINT, v3. The v1 `sendTemplateMessage` cannot name the number a template leaves from — both of
	WATI's documented spellings were sent in the body AND in the query, and all four arrived from the
	account's default number while the API answered success. v3 `messageTemplates/send` has a `channel`
	field that works. An account with one number names none and sends from the only number it has, so the
	same call serves both and there is no second path to keep in step.

	THE CORRELATION ID IS OURS HERE, and that is measured rather than assumed: v1 minted it, v3 takes
	`local_message_id` from us. Two live sends on 2026-09-08 came back as `templateMessageSent_v2` and then
	`sentMessageDELIVERED_v2`, each echoing the exact id this function generated, which is why those rows
	reached `delivered` instead of resting at `sent`. Without that echo nothing could ever tick a sent
	message, and this would have stayed on two paths.

	THE ONE OPERATOR PREREQUISITE. v3 returns no wamid, and an inbound button tap points back at one. It is
	backfilled from the `templateMessageSent_v2` event (`ingest._update_status`), so THAT EVENT MUST BE
	SUBSCRIBED on every account's webhook or a tap cannot be joined to the message that offered it. Nothing
	in the code can check a provider's dashboard; this is a go-live step, not an assumption.

	The broadcast label defaults to the clean template name, so WATI groups same-template sends rather than
	scattering them under an account-scoped record id.
	"""
	doc = _template_doc(template)
	wire = _wire_name(doc)
	return _send(
		transport.send_template_message,
		account,
		to_number=to,
		template_name=wire,
		broadcast_name=broadcast_name or f"crm_{frappe.scrub(wire)}",
		parameters=variables or [],
		channel_number=_channel_number(account),
		local_message_id=frappe.generate_hash(length=24),
	)


def send_session(account, to, text) -> contract.SendResult:
	"""Free text inside an open 24h session."""
	return _send(
		transport.send_session_message, account, to, text or "", channel_number=_channel_number(account)
	)


def send_media(account, to, filename, content, mimetype, caption="") -> contract.SendResult:
	"""Send bytes we hold. Our own File rows always go this way — the provider cannot authenticate to
	our proxy URL, so a URL send would fail."""
	return _send(
		transport.send_session_file, account, to, filename, content, mimetype, caption,
		channel_number=_channel_number(account),
	)


def send_media_url(account, to, file_url, caption="") -> contract.SendResult:
	"""Send a genuine external link — one we never held the bytes for."""
	return _send(
		transport.send_session_file_via_url, account, to, file_url, caption,
		channel_number=_channel_number(account),
	)


def list_templates(account):
	"""The account's approved templates, in the CHANNEL's shape — not WATI's.

	`[{name, language, category, body, variables}]`, where `variables` is `{param_name: sample}` in body
	order. This is the last place the provider's own dictionary exists: a vendor-free module reading
	`custom_params` would have mirrored an empty catalogue for the next provider.

	Read as the number we will SEND as, so the picker offers what that number may actually use.

	`variables` is a mapping, not a list, because WATI matches parameters by NAME and not by the position
	shown in the body — sending positional "1","2" fills the slots blank.
	"""
	items = transport.get_message_templates(account, channel_number=_channel_number(account)) or []
	return [t for t in (_template_shape(i) for i in items) if t]


def _template_shape(item) -> dict | None:
	"""One WATI template item -> the channel's template shape, or None when it is not ours to offer.

	Only an APPROVED template is offered: the picker is a list of things a rep may send, and a draft or
	rejected one answers the provider's refusal at the patient rather than at the screen.
	"""
	if (item.get("status") or "").upper() != "APPROVED":
		return None
	language = item.get("language_option") or item.get("language")
	code = (language.get("value") if isinstance(language, dict) else language) or "en"
	params = item.get("custom_params") or []
	return {
		"name": item.get("name"),
		"language": str(code).replace("-", "_"),
		"category": item.get("category") or "UTILITY",
		"body": item.get("body") or "",
		"variables": {
			str(p.get("name") or index + 1): (p.get("value") or "")
			for index, p in enumerate(params)
		},
	}


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


def history(account, contact):
	"""Every message of THIS account's conversation with `contact`, oldest page first.

	Addressed through `transport.conversation_target`, the same rule every v3 conversation call uses: a
	bare contact resolves to the person, whose thread on a multi-number account holds every number's
	messages, and rebuilding a lead from that files another programme's conversation onto the patient.
	Measured on one contact: 41 items bare, 16 channel-scoped, and only the 16 were this number's.

	The caller passes a phone number and nothing else — which number it belongs to is the account's own
	fact, and the adapter is where a provider's addressing lives.
	"""
	return list(transport.iter_conversation_messages(
		account, transport.conversation_target(contact, _channel_number(account))
	))


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
# The only two v3 event types that ARE messages. Everything else (`ticket`) is a lifecycle event with
# no body, an int `type` and a null `owner` — measured, not assumed.
_HISTORY_MESSAGE_EVENTS = ("message", "broadcastMessage")

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

	`event_type` decides WHETHER this is a message; `owner` decides its DIRECTION. Conflating the two
	cost a thread: reading direction off event_type files an agent's outbound message as a status, and
	NOT reading event_type at all forces ticket rows through the message path.

	Measured over 100 live items, the three shapes are sharply distinct:

	  message          60   type is a STRING (text/document/image), owner is a BOOL. A real message.
	  ticket           27   type is an INT (0,1,4,7,9,10), owner is None, there is no body. A ticket
	                        lifecycle event — assignment, resolution. NOT a message, and forcing one
	                        through crashes on the int the moment content_type_for() strips it.
	  broadcastMessage 13   type and owner are BOTH None; the rendered body is in final_text. An
	                        OUTBOUND template. Trusting `owner` here reads None as falsy and files the
	                        business's own template as a message FROM the patient — silently, no error.
	"""
	event_type = item.get("event_type")
	if event_type not in _HISTORY_MESSAGE_EVENTS:
		return None

	payload = {}
	for camel, snake in _HISTORY_KEYS:
		payload[camel] = item.get(snake)
	# WATI's history renders a template's body into final_text; `text` is the unsubstituted form. Prefer the rendered one, or a recovered template bubble shows {{1}} to the rep.
	payload["text"] = item.get("final_text") or item.get("text")
	payload["waId"] = number
	payload["localMessageId"] = correlation_id

	if event_type == "broadcastMessage":
		# An outbound template. `owner` is None on every one of them, so direction is decided here.
		payload["type"] = "text"
		payload["eventType"] = "templateMessageSent_v2"
	elif payload.get("owner"):  # business -> customer (an agent or bot typed it on the provider side)
		payload["type"] = payload.get("type") or "text"
		payload["eventType"] = "sessionMessageSent_v2"
	else:  # customer -> business
		payload["type"] = payload.get("type") or "text"
		payload["eventType"] = "message"
		payload["senderName"] = payload.get("operatorName")
	return normalize(payload, account)
