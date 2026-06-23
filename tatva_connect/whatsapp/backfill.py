"""WATI history backfill — the PULL half of WhatsApp ingest.

The live webhook is the real-time source, but it can miss messages (a webhook
outage, a redelivery dropped, or a conversation that predates the webhook). WATI's
`getMessages` is the authoritative pull source: it returns a number's FULL two-way
thread — including replies typed directly in the WATI portal.

This maps each history item into the SAME event shape the webhook handler reads and
feeds it through `webhook.process_event`, so ALL the lead-linking / direction /
status / media logic lives in ONE place (no second brain).

NOTE — deliberate adapter-entry asymmetry: this PULL path enters at the adapter's
parsed entry (`process_event`), NOT the spine front door — there is no live request,
token or raw-log to replay; WATI's getMessages API IS the trusted source. The live
webhook PUSH path enters via the spine. Don't "unify" these into one entry point;
the asymmetry is by design (mirror of the note in telephony/reconcile.py).

De-dup is by the WATI
message id (`custom_provider_message_id`), the one identity present on BOTH the live webhook and
the history API — so live + backfill never double-insert.

DORMANT BY DESIGN: the scheduled entry is gated by the `WhatsApp::WATI::backfill`
switch (OFF by default) and is NOT wired in hooks.py — the operator arms it by turning
the switch on and registering a Scheduled Job Type with their chosen cron. The manual
entry (`refresh_history`) defaults to a dry-run.
"""
import frappe
from frappe.utils import add_to_date, now_datetime

from tatva_connect import automation
from tatva_connect.whatsapp import api as wati
from tatva_connect.whatsapp import routing
from tatva_connect.whatsapp import webhook


def _history_item_to_event(item: dict, number: str) -> dict:
	"""Map a getMessages history item into the WATI webhook event shape.

	History reports BOTH directions as eventType "message" + an `owner` flag, and never
	carries a whatsappMessageId — so we translate it into the live event the handler
	expects: owner -> outbound `*MessageSent_v2`, else inbound `message`. The number comes
	from the query (history items omit waId)."""
	event = {
		"waId": number,
		"id": item.get("id"),  # the cross-path identity
		"whatsappMessageId": item.get("whatsappMessageId"),  # null in history
		"text": item.get("text"),
		"type": item.get("type") or "text",
		"data": item.get("data"),
		"conversationId": item.get("conversationId"),
		"statusString": item.get("statusString"),
		"owner": item.get("owner"),
	}
	if item.get("owner"):  # business -> customer (agent or bot typed it on the WATI side)
		event["eventType"] = "templateMessageSent_v2" if item.get("templateId") else "sessionMessageSent_v2"
		event["localMessageId"] = item.get("localMessageId")
		event["operatorName"] = item.get("operatorName")
	else:  # customer -> business
		event["eventType"] = "message"
		event["senderName"] = item.get("senderName") or item.get("operatorName")
	return event


def _already_have(lead: str, item: dict) -> bool:
	"""Is this history item already a row on `lead`? Match on the WATI id first (set by both
	ingest paths), then on localMessageId (covers rows WE sent, which carry it as message_id)."""
	wid = item.get("id")
	if wid and frappe.db.exists("WhatsApp Message", {"custom_provider_message_id": wid, "reference_name": lead}):
		return True
	lmid = item.get("localMessageId")
	if lmid and frappe.db.exists("WhatsApp Message", {"message_id": lmid, "reference_name": lead}):
		return True
	return False


def backfill_lead(lead_name: str, dry_run: bool = True) -> dict:
	"""Pull one lead's full WATI history and insert any message (either direction) missing
	locally. dry_run=True (default) counts only — touches nothing. Returns a summary."""
	if not wati.is_enabled():
		return {"ok": False, "reason": "WATI disabled"}
	lead = frappe.get_cached_doc("CRM Lead", lead_name)
	account = routing.resolve_account_for_lead(lead)
	if not account:
		return {"ok": False, "reason": "no WATI route for this lead"}
	number = wati.normalize_number(lead.get("mobile_no"))
	if not number:
		return {"ok": False, "reason": "lead has no mobile number"}

	account_doc = frappe.get_doc("WhatsApp Account", account)
	items = wati.get_all_messages(account_doc, number)

	summary = {"ok": True, "lead": lead_name, "scanned": 0, "new": 0, "existing": 0, "skipped": 0,
	           "dry_run": bool(dry_run)}
	for item in items:
		if item.get("eventType") != "message" or not item.get("id"):
			summary["skipped"] += 1  # tickets, call events, malformed
			continue
		summary["scanned"] += 1
		if _already_have(lead_name, item):
			summary["existing"] += 1
			continue
		summary["new"] += 1
		if not dry_run:
			# One brain: the live handler does the routing / insert / status / media + dedup.
			webhook.process_event(_history_item_to_event(item, number), account)
	if not dry_run:
		frappe.db.commit()
	return summary


@frappe.whitelist()
def refresh_history(reference_name: str, dry_run=1) -> dict:
	"""Manual entry — pull one lead's full WATI history and insert anything missing.

	Defaults to a safe dry-run (counts only). Requires WRITE access to the lead — so a caller
	who cannot see/act on the lead can't even probe its WhatsApp metadata (let alone inject
	history with dry_run=0). Gated on WATI being enabled; the operator runs this deliberately."""
	frappe.has_permission("CRM Lead", "write", doc=reference_name, throw=True)
	return backfill_lead(reference_name, dry_run=bool(int(dry_run)))


def scheduled_backfill(hours: int = 24) -> dict:
	"""Scheduler entry — top up recently-active conversations from WATI history.

	⚠️ DORMANT + NOT WIRED in hooks.py. Gated by the `WhatsApp::WATI::backfill` switch
	(OFF by default). The operator arms it: turn the switch ON and register a Scheduled
	Job Type for this method with the desired cron. A no-op until then, even if called."""
	if not automation.is_enabled("WhatsApp::WATI::backfill"):
		return {"ok": False, "reason": "WhatsApp::WATI::backfill disabled"}
	since = add_to_date(now_datetime(), hours=-int(hours))
	leads = frappe.get_all(
		"WhatsApp Message",
		filters={"reference_doctype": "CRM Lead", "modified": [">=", since]},
		distinct=True,
		pluck="reference_name",
	)
	summary = {"ok": True, "leads": 0, "new": 0}
	for lead in leads:
		if not lead:
			continue
		summary["leads"] += 1
		res = backfill_lead(lead, dry_run=False)
		if isinstance(res, dict):
			summary["new"] += res.get("new", 0)
	return summary
