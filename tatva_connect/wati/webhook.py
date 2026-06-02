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
    https://<host>/api/method/tatva_connect.wati.webhook.webhook?token=<secret>
where <secret> == WATI Settings.webhook_verify_token.
"""
import frappe

from tatva_connect.wati import api as wati

# WATI event names that carry a delivery/read status, keyed by localMessageId.
STATUS_EVENTS = {
	"templateMessageSent_v2",
	"sentMessageDELIVERED_v2",
	"sentMessageREAD_v2",
	"sentMessageREPLIED_v2",
	"templateMessageFailed",
}

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


def _default_wati_account():
	return frappe.db.get_value("WhatsApp Account", {"custom_is_wati": 1}, "name")


def _is_crm_relevant(event: dict) -> bool:
	"""Cheap membership filter — runs inline before we enqueue anything."""
	if event.get("eventType") == "message" and _falsy(event.get("owner")):
		return bool(_lead_for_number(wati.normalize_number(event.get("waId"))))
	if event.get("localMessageId"):
		return bool(frappe.db.exists("WhatsApp Message", {"message_id": event.get("localMessageId")}))
	return False


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
	expected = frappe.db.get_single_value("WATI Settings", "webhook_verify_token")
	if expected:
		token = frappe.request.args.get("token") if frappe.request else None
		token = token or frappe.form_dict.get("token")
		if token != expected:
			raise frappe.PermissionError("Invalid WATI webhook token")

	# Flat JSON payload -> plain dict (drop Frappe/query keys).
	event = {k: v for k, v in frappe.form_dict.items() if k not in ("cmd", "token")}

	# Membership filter: drop non-CRM traffic with a 200 (zero rows written).
	if not _is_crm_relevant(event):
		return "ok"

	# Offload the survivor; return immediately.
	# NB: 'payload' (not 'event') — 'event' is a reserved kwarg of frappe.enqueue
	# and would be swallowed instead of forwarded to the job.
	frappe.enqueue(
		"tatva_connect.wati.webhook.process_event",
		queue="short",
		payload=event,
	)
	return "ok"


def process_event(payload: dict):
	"""Background worker: persist one CRM-relevant event."""
	if payload.get("eventType") == "message" and _falsy(payload.get("owner")):
		_ingest_inbound(payload)
	elif payload.get("localMessageId"):
		_update_status(payload)


def _ingest_inbound(event: dict):
	wamid = event.get("whatsappMessageId")
	# Idempotent: WATI redelivers.
	if wamid and frappe.db.exists("WhatsApp Message", {"message_id": wamid}):
		return
	lead = _lead_for_number(wati.normalize_number(event.get("waId")))
	if not lead:
		return
	frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"type": "Incoming",
			"from": event.get("waId"),
			"message": event.get("text"),
			"content_type": event.get("type") or "text",
			"message_id": wamid,
			"conversation_id": event.get("conversationId"),
			"profile_name": event.get("senderName"),
			"whatsapp_account": _default_wati_account(),
			"reference_doctype": "CRM Lead",
			"reference_name": lead,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def _update_status(event: dict):
	row = frappe.db.get_value("WhatsApp Message", {"message_id": event.get("localMessageId")}, "name")
	if not row:
		return
	status = event.get("statusString")
	if status:
		frappe.db.set_value("WhatsApp Message", row, "status", status, update_modified=False)
		frappe.db.commit()
