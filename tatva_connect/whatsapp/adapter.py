"""WATI webhook adapter — the per-vendor brains behind the shared ingress spine.

The spine (`tatva_connect.webhooks.spine`) owns the front door (auth · raw-log ·
fast-ACK · enqueue · dedupe-dispatch). This module is the WATI-specific half: it
decides relevance, idempotency, and the actual `WhatsApp Message` DB moves. Logic
moved here unchanged from the old `whatsapp/webhook.py` handler — the 1,519 captured
WATI payloads ingest byte-identically.

Duck-typed adapter contract (no ABC):
  * is_relevant(payload, event, account) -> bool      — cheap front-door pre-filter
  * already_processed(payload, event, account) -> bool — idempotency vs WhatsApp Message
  * handle(payload, event, account) -> None            — dispatch on eventType + clean DB moves
  * account_for_payload(payload, event) -> account|None — re-derive account on replay (fail-closed)

WATI uses no vendor sub-event, so the spine's `event` is always None here — the WATI
event family comes from the payload's own `eventType`. Idempotent on the WATI message
id; inbound names on whatsappMessageId, outbound de-dupes on custom_provider_message_id
(the id present on BOTH the live webhook and the getMessages history, so live + backfill
never collide). No Meta anywhere.
"""
import frappe

from tatva_connect.whatsapp import api as wati
from tatva_connect.whatsapp import media as media_module
from tatva_connect.whatsapp import routing

# WATI status event -> the status vocab the CRM WhatsApp tab renders
# (sent/Success -> single tick; delivered/read -> double tick, read = blue;
# failed -> error). Driven by eventType so a missing `statusString` still maps.
STATUS_BY_EVENT = {
	"templateMessageSent_v2": "sent",
	"sentMessageDELIVERED_v2": "delivered",
	"sentMessageREAD_v2": "read",
	"sentMessageREPLIED_v2": "read",
	"templateMessageFailed": "failed",
}
STATUS_EVENTS = set(STATUS_BY_EVENT)

# Outbound "Sent" events for messages that originated OUTSIDE Frappe — an agent or bot
# typed them in the WATI portal. WATI emits both a v1 and a v2 of every event; only the v2
# carries localMessageId, so we ingest ONLY the v2 (taking both would double-insert).
# templateMessageSent_v2 doubles as the "sent" status for OUR OWN template sends — the
# handler decides per message which case applies (status update vs new-message ingest).
OUTBOUND_SENT_EVENTS = {"sessionMessageSent_v2", "templateMessageSent_v2"}

# WATI's history (getMessages) reports the live status as a string, not an eventType.
_STATUS_BY_STRING = {"SENT": "sent", "DELIVERED": "delivered", "READ": "read", "REPLIED": "read"}

_FALSY = {False, "false", "False", 0, "0", None, ""}


def _falsy(v):
	return v in _FALSY


def _status_from_string(status_string):
	"""WATI `statusString` (SENT/DELIVERED/READ/REPLIED) -> the CRM WhatsApp status vocab."""
	return _STATUS_BY_STRING.get((status_string or "").upper())


def _lead_for_number(wa_digits: str):
	"""Find a CRM lead by normalized number. Leads are stored E.164 ('+<digits>')."""
	if not wa_digits:
		return None
	return frappe.db.get_value("CRM Lead", {"mobile_no": "+" + wa_digits}, "name") or frappe.db.get_value(
		"CRM Lead", {"mobile_no": wa_digits}, "name"
	)


# ---------------------------------------------------------------------------
# Adapter contract — duck-typed, called by the spine.
# ---------------------------------------------------------------------------
def is_relevant(payload, event=None, account=None) -> bool:
	"""Cheap membership filter — runs inline before we enqueue anything."""
	ev = payload.get("eventType")
	if ev == "message" and _falsy(payload.get("owner")):
		return bool(_lead_for_number(wati.normalize_number(payload.get("waId"))))
	if ev in OUTBOUND_SENT_EVENTS:
		# Either a status confirmation for a row WE sent (matched by localMessageId), or a
		# portal/bot message to ingest (matched by a CRM lead on the number). The precise,
		# account-scoped routing happens in the worker — this is just the cheap pre-filter.
		lmid = payload.get("localMessageId")
		if lmid and frappe.db.exists("WhatsApp Message", {"message_id": lmid}):
			return True
		return bool(_lead_for_number(wati.normalize_number(payload.get("waId"))))
	if payload.get("localMessageId"):
		return bool(frappe.db.exists("WhatsApp Message", {"message_id": payload.get("localMessageId")}))
	return False


def already_processed(payload, event=None, account=None) -> bool:
	"""Idempotency — WATI redelivers. Prefer the wamid; fall back to a composite
	key (conversation + sender + text) when the payload lacks one."""
	wamid = payload.get("whatsappMessageId")
	if wamid:
		return bool(frappe.db.exists("WhatsApp Message", {"message_id": wamid}))
	return bool(
		frappe.db.exists(
			"WhatsApp Message",
			{
				"type": "Incoming",
				"conversation_id": payload.get("conversationId"),
				"from": payload.get("waId"),
				"message": payload.get("text"),
			},
		)
	)


def handle(payload, event=None, account=None) -> None:
	"""Persist one CRM-relevant WATI event. Dispatch on the payload's eventType to the
	per-direction ingest. Runs in the worker (privileged); the spine already gated what
	reaches here via the token + is_relevant pre-filter."""
	ev = payload.get("eventType")
	if ev == "message" and _falsy(payload.get("owner")):
		_ingest_inbound(payload, account)
	elif ev in OUTBOUND_SENT_EVENTS:
		_ingest_outbound(payload, account)
	elif payload.get("localMessageId"):
		_update_status(payload)


def account_for_payload(payload, event=None):
	"""Replay hook: re-derive the receiving WATI account from a STORED payload.

	The live front door resolves the account from the URL token (not a payload field —
	WATI inbound carries no reliable tenant id), so a replayed payload has no token. We
	fall back to the only account-bearing identity in the stored row: a localMessageId
	already pinned to a WhatsApp Message tells us the account we sent/ingested it under.
	No such anchor -> None, and the ingest helpers fail closed (account-less ingest is a
	no-op + drop)."""
	lmid = payload.get("localMessageId")
	if lmid:
		return frappe.db.get_value("WhatsApp Message", {"message_id": lmid}, "whatsapp_account")
	wamid = payload.get("whatsappMessageId")
	if wamid:
		return frappe.db.get_value("WhatsApp Message", {"message_id": wamid}, "whatsapp_account")
	return None


# ---------------------------------------------------------------------------
# Per-direction ingest — clean DB moves + fail-closed attribution (moved unchanged).
# ---------------------------------------------------------------------------
def _fetch_media(event: dict, account):
	"""Download a WATI media file (image/document/…) using the receiving account's token,
	or None for a text event / a failed download. Returns the (content, data, type, text)
	tuple the insert helpers expect. Shared by inbound + outbound ingest."""
	if event.get("type") not in media_module._MEDIA_TYPES or not event.get("data"):
		return None
	try:
		content, _ctype = wati.get_media(frappe.get_doc("WhatsApp Account", account), event["data"])
		return (content, event["data"], event.get("type"), event.get("text"))
	except Exception:
		frappe.log_error(title="WATI media download failed",
		                 message=f"id={event.get('id')} type={event.get('type')}")
		return None


def _ingest_inbound(event: dict, account):
	if already_processed(event):
		return
	sender = wati.normalize_number(event.get("waId"))  # digits, no '+'
	if not sender or not account:
		return

	# Strict: attach only to leads on this number whose taxonomy routes to the receiving
	# account. No hit -> DROP (1-line log); never best-guess to a lead on another account.
	candidates = routing.candidates_for_number("+" + sender)
	targets = routing.leads_for_number_and_account(candidates, account)
	if not targets:
		frappe.log_error(title="WATI inbound dropped: no lead routes to the receiving account",
		                 message=f"waId={event.get('waId')} account={account}")
		return

	wid = event.get("whatsappMessageId")
	media = _fetch_media(event, account)  # account token always known here
	wid_media = event.get("id")  # WATI internal id — the File↔history join key
	for lead in targets:
		_insert_inbound_row(event, account, lead, wid, media=media, wid_media=wid_media)
	frappe.db.commit()

	# crm publishes the "whatsapp_message" realtime event in its on_update hook — which
	# fires DURING insert, BEFORE this commit. A browser reloading on that event reads the
	# row before it is committed, so the chat tab lags one message behind. Re-emit the same
	# event AFTER the commit so the reload fetches committed data and the bubble shows now.
	for lead in targets:
		frappe.publish_realtime(
			"whatsapp_message", {"reference_doctype": "CRM Lead", "reference_name": lead}
		)


def _insert_inbound_row(event: dict, account, lead, wid, media=None, wid_media=None):
	"""Insert one inbound row for `lead`. Name is scoped per-lead ({lead}-{wid}) — the
	SAME scheme reconcile uses — so the same message mirrors onto >1 same-account lead
	without colliding on the PK / composite index. Per-lead idempotent."""
	name = f"{lead}-{wid}" if wid else None
	if name and frappe.db.exists("WhatsApp Message", name):
		return
	doc = frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"type": "Incoming",
			"from": event.get("waId"),
			"message": event.get("text"),
			"content_type": event.get("type") or "text",
			"message_id": wid,
			"custom_provider_message_id": event.get("id"),  # cross-path identity (live + history backfill)
			"conversation_id": event.get("conversationId"),
			"profile_name": event.get("senderName"),
			"whatsapp_account": account,
			"reference_doctype": "CRM Lead",
			"reference_name": lead,
		}
	)
	if media:
		content, data, mtype, text = media
		fname = media_module.media_filename(mtype, text, data)
		filedoc = media_module.ensure_lead_media(lead, wid_media, fname, content)
		doc.content_type = mtype
		doc.attach = filedoc.file_url          # proxy URL → bubble renders; linker skips it (contract C)
		doc.message = text if mtype == "image" else (doc.message or "")
	if name:
		doc.name = name
		doc.flags.name_set = True
	doc.flags.tatva_pinned_lead = lead  # restored in before_save (see pin_inbound_reference)
	doc.insert(ignore_permissions=True)


def _ingest_outbound(event: dict, account):
	"""Persist an outbound message that originated OUTSIDE Frappe — an agent or bot typed it
	in the WATI portal. Mirror of _ingest_inbound for the other direction.

	If the message is one WE sent (or already ingested), a row already carries its
	localMessageId — then this event is only a delivery-status confirmation, not a new
	message, so route it to _update_status instead of inserting a duplicate."""
	lmid = event.get("localMessageId")
	if lmid and frappe.db.exists("WhatsApp Message", {"message_id": lmid}):
		_update_status(event)
		return

	sender = wati.normalize_number(event.get("waId"))  # the customer's number, digits
	if not sender or not account:
		return

	# Identical account-scoped attribution to inbound: attach only to leads on this number
	# whose taxonomy routes to the receiving account. No hit -> DROP + log, never best-guess.
	candidates = routing.candidates_for_number("+" + sender)
	targets = routing.leads_for_number_and_account(candidates, account)
	if not targets:
		frappe.log_error(title="WATI outbound dropped: no lead routes to the receiving account",
		                 message=f"waId={event.get('waId')} account={account}")
		return

	wid = event.get("id")  # the cross-path identity — history carries no whatsappMessageId
	media = _fetch_media(event, account)
	for lead in targets:
		_insert_outbound_row(event, account, lead, wid, media=media, wid_media=wid)
	frappe.db.commit()

	# Same post-commit realtime re-emit as inbound, so the tab updates without a reload.
	for lead in targets:
		frappe.publish_realtime(
			"whatsapp_message", {"reference_doctype": "CRM Lead", "reference_name": lead}
		)


def _insert_outbound_row(event: dict, account, lead, wid, media=None, wid_media=None):
	"""Insert one Outgoing row mirroring a WATI-side message onto `lead`. Per-lead idempotent
	on the WATI id (custom_provider_message_id) — the ONE key shared by the live webhook and the history
	backfill, so the two paths never double-insert the same message."""
	if not wid:
		return
	if frappe.db.exists("WhatsApp Message", {"custom_provider_message_id": wid, "reference_name": lead}):
		return
	name = f"{lead}-{wid}"
	if frappe.db.exists("WhatsApp Message", name):
		return

	# The Sent event already carries the fully-rendered body in `text` (template variables
	# substituted), so store it as a plain Manual bubble — we're mirroring a message, not
	# re-sending a template, and "Template" without a `template` link renders blank.
	status = STATUS_BY_EVENT.get(event.get("eventType")) or _status_from_string(event.get("statusString"))
	doc = frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"type": "Outgoing",
			"to": event.get("waId"),
			"message": event.get("text"),
			"content_type": event.get("type") or "text",
			"message_type": "Manual",
			"message_id": event.get("localMessageId"),  # lets later DELIVERED/READ events tick this row
			"custom_provider_message_id": wid,
			"conversation_id": event.get("conversationId"),
			"profile_name": event.get("operatorName"),  # who sent it on the WATI side (agent / "Bot")
			"status": status,
			"whatsapp_account": account,
			"reference_doctype": "CRM Lead",
			"reference_name": lead,
		}
	)
	if media:
		content, data, mtype, text = media
		fname = media_module.media_filename(mtype, text, data)
		filedoc = media_module.ensure_lead_media(lead, wid_media, fname, content)
		doc.content_type = mtype
		doc.attach = filedoc.file_url
		doc.message = text if mtype == "image" else (doc.message or "")
	doc.name = name
	doc.flags.name_set = True
	doc.flags.tatva_ingested = True  # mirror of an existing WATI message — controller must not re-send
	doc.insert(ignore_permissions=True)


def _update_status(event: dict):
	# A shared number can mirror the same message onto >1 lead (composite identity),
	# so update EVERY row carrying this message_id, not just the first.
	rows = frappe.get_all(
		"WhatsApp Message",
		filters={"message_id": event.get("localMessageId")},
		pluck="name",
	)
	if not rows:
		return
	# Map by eventType (robust to a missing statusString — e.g. failures).
	status = STATUS_BY_EVENT.get(event.get("eventType"))
	if not status:
		return
	for row in rows:
		frappe.db.set_value("WhatsApp Message", row, "status", status, update_modified=False)
	frappe.db.commit()
