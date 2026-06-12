"""WATI inbound webhook (guest endpoint).

WATI POSTs the ENTIRE tenant's traffic here (a shared-tenant firehose), so the
handler must be cheap and safe:

  verify secret -> kill-switch -> membership-filter (drop non-CRM with 200)
  -> enqueue the few survivors -> return 200 immediately.

Heavy work (insert / status update) runs on the background worker. We persist
only:
  * inbound customer messages (eventType="message", owner falsy) whose waId
    matches a CRM lead;
  * status events whose localMessageId matches a row WE sent.
Everything else is dropped. Idempotent on whatsappMessageId (WATI redelivers).

Inbound rows land in `WhatsApp Message` linked to the lead, so they render in the
lead's WhatsApp tab automatically. No Meta anywhere.

Register on each WATI dashboard the pretty, provider-uniform URL:
    https://<host>/webhooks/whatsapp/wati/<token>
where <token> == that account's `custom_webhook_token`. nginx rewrites the trailing
segment to `?token=` (see nginx/frappe.conf.template); the token both authenticates the
caller and identifies the receiving account (routing.account_by_token) — inbound never
depends on a WATI payload field. Setup: vault runbook 02-operations/runbooks/09.
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

_FALSY = {False, "false", "False", 0, "0", None, ""}


def _falsy(v):
	return v in _FALSY


def _lead_for_number(wa_digits: str):
	"""Find a CRM lead by normalized number. Leads are stored E.164 ('+<digits>')."""
	if not wa_digits:
		return None
	return frappe.db.get_value("CRM Lead", {"mobile_no": "+" + wa_digits}, "name") or frappe.db.get_value(
		"CRM Lead", {"mobile_no": wa_digits}, "name"
	)


def _is_crm_relevant(event: dict) -> bool:
	"""Cheap membership filter — runs inline before we enqueue anything."""
	if event.get("eventType") == "message" and _falsy(event.get("owner")):
		return bool(_lead_for_number(wati.normalize_number(event.get("waId"))))
	if event.get("localMessageId"):
		return bool(frappe.db.exists("WhatsApp Message", {"message_id": event.get("localMessageId")}))
	return False


def _debug_log_payload(event: dict):
	import json

	try:
		frappe.get_doc({
			"doctype": "Integration Request",
			"integration_request_service": "WATI",
			"request_description": "WATI inbound webhook",
			"status": "Completed",
			"data": json.dumps(event, default=str),
		}).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		pass  # debug logging must never break the webhook


@frappe.whitelist(allow_guest=True)
def webhook(**kwargs):
	"""Fast-ack endpoint. Always returns quickly; never blocks on heavy work."""
	# Kill-switch: silently accept and ignore everything when disabled.
	# Fresh DB read (not get_cached_doc) so flipping the switch takes effect
	# immediately across all worker processes.
	if not wati.is_enabled():
		return "ok"

	# Auth + identity in ONE step: the token in the URL IS the receiving account's
	# secret. nginx maps /webhooks/whatsapp/wati/<token> -> ?token=<token>; resolving the
	# account from it both rejects impostors and tells us which tenant received the message
	# (WATI inbound carries no reliable tenant id — see routing.account_by_token). For a
	# JSON POST, form_dict holds the body, so read the token from request.args (the query).
	token = frappe.request.args.get("token") if frappe.request else None
	token = token or frappe.form_dict.get("token")
	account = routing.account_by_token(token)
	if not account:
		raise frappe.PermissionError("Invalid WATI webhook token")

	# Flat JSON payload -> plain dict (drop Frappe/query keys).
	event = {k: v for k, v in frappe.form_dict.items() if k not in ("cmd", "token")}

	# Debug capture — operator toggle, default OFF (CRM WATI Settings.debug_log_payloads).
	# Off = nothing stored (the webhook only ever translates payloads into WhatsApp
	# Message rows). On = raw payload -> core Integration Request (service "WATI",
	# 90-day auto-clear). Token already verified above; never raises.
	if frappe.db.get_single_value("CRM WATI Settings", "debug_log_payloads"):
		_debug_log_payload(event)

	# Membership filter: drop non-CRM traffic with a 200 (zero rows written).
	if not _is_crm_relevant(event):
		return "ok"

	# Offload the survivor; return immediately. The account is already resolved (from the
	# token above) — pass it through so the worker never re-guesses the tenant.
	# NB: 'payload' (not 'event') — 'event' is a reserved kwarg of frappe.enqueue
	# and would be swallowed instead of forwarded to the job.
	frappe.enqueue(
		"tatva_connect.whatsapp.webhook.process_event",
		queue="short",
		payload=event,
		account=account,
	)
	return "ok"


@frappe.whitelist()
def webhook_urls():
	"""Admin helper: the exact pretty webhook URL to register on each WATI dashboard —
	provider-uniform style /webhooks/whatsapp/wati/<token>, where <token> is THAT account's
	`custom_webhook_token`. nginx rewrites the trailing segment to ?token=; the handler
	resolves the account from it (routing.account_by_token). System Manager only (default
	@frappe.whitelist gating). Returns {account: url|None}; None = token not set yet."""
	from frappe.utils import get_url

	host = get_url().rstrip("/")
	out = {}
	for acc in frappe.get_all(
		"WhatsApp Account", filters={"custom_is_wati": 1}, fields=["name", "custom_webhook_token"]
	):
		out[acc.name] = (
			f"{host}/webhooks/whatsapp/wati/{acc.custom_webhook_token}"
			if acc.custom_webhook_token
			else None
		)
	return out


def process_event(payload: dict, account=None):
	"""Background worker: persist one CRM-relevant event.

	`account` is the tenant resolved from the webhook token (authoritative). Runs as a
	system user: the webhook is a guest endpoint, but persistence (and any downstream
	automation like assigning a follow-up task) must run privileged — otherwise native
	CRM Task assignment hits a Guest PermissionError. The token + membership filter in
	webhook() already gate what reaches here.
	"""
	if frappe.session.user == "Guest":
		frappe.set_user("Administrator")

	if payload.get("eventType") == "message" and _falsy(payload.get("owner")):
		_ingest_inbound(payload, account)
	elif payload.get("localMessageId"):
		_update_status(payload)


def _already_ingested(event: dict) -> bool:
	"""Idempotency — WATI redelivers. Prefer the wamid; fall back to a composite
	key (conversation + sender + text) when the payload lacks one."""
	wamid = event.get("whatsappMessageId")
	if wamid:
		return bool(frappe.db.exists("WhatsApp Message", {"message_id": wamid}))
	return bool(
		frappe.db.exists(
			"WhatsApp Message",
			{
				"type": "Incoming",
				"conversation_id": event.get("conversationId"),
				"from": event.get("waId"),
				"message": event.get("text"),
			},
		)
	)


def _ingest_inbound(event: dict, account):
	if _already_ingested(event):
		return
	sender = wati.normalize_number(event.get("waId"))  # digits, no '+'
	if not sender or not account:
		return

	# Account-aware: attach to every lead sharing this conversation (phone + account).
	targets = routing.leads_for_number_and_account("+" + sender, account)

	# Fallback — the account is known (from the token), but no lead routes to it for this
	# number (e.g. the number's lead lives on a different account only). Never silently drop
	# a real customer message: attach to the first lead by phone and log the mismatch.
	if not targets:
		first = _lead_for_number(sender)
		if not first:
			return
		targets = [first]
		frappe.log_error(
			title="WATI inbound: no lead routes to the receiving account, fell back to first lead",
			message=f"waId={event.get('waId')} account={account} attached_to={first}",
		)

	wid = event.get("whatsappMessageId")

	# Media downloads use the receiving account's WATI token (always known here).
	media = None
	if event.get("type") in media_module._MEDIA_TYPES and event.get("data"):
		try:
			content, _ctype = wati.get_media(frappe.get_doc("WhatsApp Account", account), event["data"])
			media = (content, event["data"], event.get("type"), event.get("text"))
		except Exception:
			frappe.log_error(title="WATI inbound media download failed",
			                 message=f"id={event.get('id')} type={event.get('type')}")
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


def pin_inbound_reference(doc, method=None):
	"""before_save: restore the account-matched lead that crm.api.whatsapp.validate
	overwrote with first-lead-by-phone. crm registers an unconditional `validate`
	doc_event on WhatsApp Message that rewrites reference_name from the phone; Frappe
	runs validate BEFORE before_save, so re-pinning here wins — and runs before the row
	is written and before crm's on_update realtime, so both use the right lead.

	Flag-gated + Incoming-only: never touches outbound (no flag) or reconcile (db_insert,
	no validate). Does not alter routing/account logic."""
	pinned = doc.flags.get("tatva_pinned_lead")
	if pinned and (doc.type or "") == "Incoming":
		doc.reference_doctype = "CRM Lead"
		doc.reference_name = pinned


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
