"""History backfill — the PULL half of WhatsApp ingest.

The live webhook is the real-time source, but it can miss messages (a webhook outage, a redelivery
dropped, or a conversation that predates the webhook). A provider's history endpoint is the
authoritative pull source: it returns a number's FULL two-way thread, including replies typed directly
in the provider's own portal.

Each history item is normalized by the SAME adapter that normalizes the live webhook, into the SAME
`ChannelEvent`, and applied by the SAME `ingest` — so all the lead-linking / direction / status / media
logic lives in ONE place and live and backfill cannot drift. The provider read underneath is the one
`recovery` and `refresh_history` use too: ONE read path, three ways in.

It enters at `ingest.apply_historical`, not `ingest.apply`: a message pulled out of the past is filed
in full and starts nothing — no journey, no notification. Backfilling a month of history must not
raise a month of follow-up tasks.

NOTE — deliberate adapter-entry asymmetry: this PULL path enters at the adapter's normalize, NOT the
spine front door — there is no live request, token or raw log to replay; the history API IS the trusted
source. The live webhook PUSH path enters via the spine. Don't "unify" these into one entry point; the
asymmetry is by design (mirror of the note in telephony/reconcile.py).

De-dup is by the provider's own message id (`custom_provider_message_id`), the one identity present on
BOTH the live webhook and the history API — so live and backfill never double-insert.

DORMANT BY DESIGN: the scheduled entry is gated by the `WhatsApp::Channel::reconcile` switch (OFF by default)
and is NOT wired in hooks.py — the operator arms it by turning the switch on and registering a
Scheduled Job Type with their chosen cron. The manual entry (`refresh_history`) defaults to a dry-run.
"""
import frappe
from frappe.utils import add_to_date, now_datetime

from tatva_connect import automation, phone
from tatva_connect.channels import resolve
from tatva_connect.whatsapp import channel, ingest, routing


def backfill_lead(lead_name: str, dry_run: bool = True) -> dict:
	"""Pull one lead's full history and insert any message (either direction) missing locally.
	dry_run=True (default) counts only — touches nothing. Returns a summary."""
	if not channel.is_enabled():
		return {"ok": False, "reason": "WhatsApp is switched off"}
	lead = frappe.get_cached_doc("CRM Lead", lead_name)
	account = routing.resolve_account_for_lead(lead)
	if not account:
		return {"ok": False, "reason": "no WhatsApp route for this lead"}
	number = phone.match_digits(lead.get("mobile_no"))
	if not number:
		return {"ok": False, "reason": "lead has no mobile number"}

	account_doc = frappe.get_doc("WhatsApp Account", account)
	adapter = resolve.adapter_for(account_doc)
	items = adapter.history(account_doc, number)

	summary = {"ok": True, "lead": lead_name, "scanned": 0, "new": 0, "existing": 0, "skipped": 0,
	           "dry_run": bool(dry_run)}
	for item in items:
		# The adapter decides what is a message: an item it cannot normalize (a ticket, a call event, something malformed) yields no event, and that IS the skip test — no second filter here.
		event = adapter.normalize_history(item, account=account, number=number)
		if not event:
			summary["skipped"] += 1
			continue
		summary["scanned"] += 1
		if ingest.held_by_lead(lead_name, event):
			summary["existing"] += 1
			continue
		summary["new"] += 1
		if not dry_run:
			# One brain: normalized by the adapter, persisted by the channel's own ingest — the same two calls the live webhook worker makes, minus the entry triggers a past message must not fire.
			ingest.apply_historical(event)
	if not dry_run:
		frappe.db.commit()
	return summary


@frappe.whitelist()
def refresh_history(reference_name: str, dry_run=1) -> dict:
	"""Manual entry — pull one lead's full history and insert anything missing.

	Defaults to a safe dry-run (counts only). Requires WRITE access to the lead — so a caller who
	cannot see or act on the lead can't even probe its WhatsApp metadata (let alone inject history with
	dry_run=0). Gated on WhatsApp being enabled; the operator runs this deliberately."""
	from crm.api.whatsapp import validate_access

	validate_access()  # WhatsApp capability gate — pulling history is a WhatsApp action
	frappe.has_permission("CRM Lead", "write", doc=reference_name, throw=True)
	return backfill_lead(reference_name, dry_run=bool(int(dry_run)))


def scheduled_backfill(hours: int = 24) -> dict:
	"""Scheduler entry — top up recently-active conversations from provider history.

	⚠️ DORMANT + NOT WIRED in hooks.py. Gated by the `WhatsApp::Channel::reconcile` switch (OFF by default). The
	operator arms it: turn the switch ON and register a Scheduled Job Type for this method with the
	desired cron. A no-op until then, even if called."""
	if not automation.is_enabled(channel.SWITCH_RECONCILE):
		return {"ok": False, "reason": f"{channel.SWITCH_RECONCILE} disabled"}
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
