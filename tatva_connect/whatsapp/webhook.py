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

Register the URL on WATI as:
    https://<host>/api/method/tatva_connect.whatsapp.webhook.webhook?token=<secret>
where <secret> == WATI Settings.webhook_verify_token.
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


def _account_for_channel(channel_number, account_hint=None):
	"""Map the inbound message -> its WhatsApp Account (hint > channel > single-tenant)."""
	from tatva_connect.whatsapp import routing

	return routing.account_for_channel(channel_number, account_hint)


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

	# Shared secret (query param on the registered URL). Reject impostors.
	# For a JSON POST, Frappe's form_dict holds the JSON body, NOT the query
	# string — so read the token from request.args (the URL query).
	expected = frappe.db.get_single_value("CRM WATI Settings", "webhook_verify_token")
	if expected:
		token = frappe.request.args.get("token") if frappe.request else None
		token = token or frappe.form_dict.get("token")
		if token != expected:
			raise frappe.PermissionError("Invalid WATI webhook token")

	# Flat JSON payload -> plain dict (drop Frappe/query keys).
	event = {k: v for k, v in frappe.form_dict.items() if k not in ("cmd", "token", "account")}

	# Debug capture — operator toggle, default OFF (CRM WATI Settings.debug_log_payloads).
	# Off = nothing stored (the webhook only ever translates payloads into WhatsApp
	# Message rows). On = raw payload -> core Integration Request (service "WATI",
	# 90-day auto-clear). Token already verified above; never raises.
	if frappe.db.get_single_value("CRM WATI Settings", "debug_log_payloads"):
		_debug_log_payload(event)

	# Membership filter: drop non-CRM traffic with a 200 (zero rows written).
	if not _is_crm_relevant(event):
		return "ok"

	# Which tenant received this? Read from the per-tenant webhook URL
	# (?account=<WhatsApp Account name>), operator-controlled — so inbound
	# attribution never depends on an unverified WATI payload field.
	account_hint = frappe.request.args.get("account") if frappe.request else None

	# Offload the survivor; return immediately.
	# NB: 'payload' (not 'event') — 'event' is a reserved kwarg of frappe.enqueue
	# and would be swallowed instead of forwarded to the job.
	frappe.enqueue(
		"tatva_connect.whatsapp.webhook.process_event",
		queue="short",
		payload=event,
		account_hint=account_hint,
	)
	return "ok"


@frappe.whitelist()
def webhook_urls():
	"""Admin helper: the exact per-account webhook URL to register on each WATI
	dashboard. Each URL carries ?account=<WhatsApp Account name> (precedence #1 in
	account_for_channel) so inbound attribution never depends on a WATI payload field.
	System Manager only (default @frappe.whitelist gating). Returns {account: url}."""
	from urllib.parse import quote

	from frappe.utils import get_url

	token = frappe.db.get_single_value("CRM WATI Settings", "webhook_verify_token") or ""
	host = get_url().rstrip("/")
	base = f"{host}/api/method/tatva_connect.whatsapp.webhook.webhook"
	out = {}
	for account in frappe.get_all("WhatsApp Account", filters={"custom_is_wati": 1}, pluck="name"):
		out[account] = f"{base}?token={quote(token)}&account={quote(account)}"
	return out


def process_event(payload: dict, account_hint=None):
	"""Background worker: persist one CRM-relevant event.

	Runs as a system user: the webhook is a guest endpoint, but persistence (and any
	downstream automation like assigning a follow-up task) must run privileged —
	otherwise native CRM Task assignment hits a Guest PermissionError. The token +
	membership filter in webhook() already gate what reaches here.
	"""
	if frappe.session.user == "Guest":
		frappe.set_user("Administrator")

	if payload.get("eventType") == "message" and _falsy(payload.get("owner")):
		_ingest_inbound(payload, account_hint)
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


def _ingest_inbound(event: dict, account_hint=None):
	if _already_ingested(event):
		return
	sender = wati.normalize_number(event.get("waId"))  # digits, no '+'
	if not sender:
		return
	account = _account_for_channel(event.get("channelPhoneNumber"), account_hint)

	# Account-aware: attach to every lead sharing this conversation (phone + account).
	targets = []
	if account:
		targets = routing.leads_for_number_and_account("+" + sender, account)

	# Fallbacks — never silently drop a real customer message:
	#  - account unknown (2+ tenants, no hint/channel match), or
	#  - account known but no lead on it (e.g. lead on a different account only).
	if not targets:
		first = _lead_for_number(sender)
		if not first:
			return
		targets = [first]
		frappe.log_error(
			title="WATI inbound: account-unmatched, fell back to first lead",
			message=f"waId={event.get('waId')} channel={event.get('channelPhoneNumber')} "
			f"hint={account_hint} resolved_account={account} attached_to={first}",
		)

	wid = event.get("whatsappMessageId")

	# The token that downloads the media is the matched account's. If inbound
	# attribution fell back (account is None: 2+ accounts, no channel/hint match),
	# resolve the account from the lead so we still have a credential.
	dl_account = account or (
		routing.resolve_account_for_lead(frappe.get_cached_doc("CRM Lead", targets[0])) if targets else None
	)
	media = None
	if dl_account and event.get("type") in media_module._MEDIA_TYPES and event.get("data"):
		try:
			content, _ctype = wati.get_media(frappe.get_doc("WhatsApp Account", dl_account), event["data"])
			media = (content, event["data"], event.get("type"), event.get("text"))
		except Exception:
			frappe.log_error(title="WATI inbound media download failed",
			                 message=f"id={event.get('id')} type={event.get('type')}")
	wid_media = event.get("id")  # WATI internal id — the File↔history join key
	for lead in targets:
		_insert_inbound_row(event, account, lead, wid, media=media, wid_media=wid_media)
	frappe.db.commit()


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
