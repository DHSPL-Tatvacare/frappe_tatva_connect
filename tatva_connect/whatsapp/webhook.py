"""WATI inbound webhook — the thin endpoint on the shared ingress spine.

WATI POSTs the ENTIRE tenant's traffic here (a shared-tenant firehose), so the
endpoint must be cheap and safe. ALL of that — kill-switch, token auth + account
scoping, always-on raw log, cheap relevance pre-filter, fast 2xx ACK + enqueue,
and the dedupe-then-handle worker — now lives in ONE place: the spine
(`tatva_connect.webhooks.spine`). The WATI-specific brains (relevance, idempotency,
the `WhatsApp Message` DB moves) live in `tatva_connect.whatsapp.adapter`.

This module keeps only the WATI-shaped surface the rest of the app references:
  * webhook()              — the guest endpoint; delegates to spine.receive("WATI", …)
  * webhook_urls()         — admin helper: the pretty URL to register per WATI dashboard
  * pin_inbound_reference()— before_save hook on WhatsApp Message (wired in hooks.py)
  * process_event()        — one-brain entry reused by the history backfill (backfill.py)

Register on each WATI dashboard the pretty, provider-uniform URL:
    https://<host>/webhooks/whatsapp/wati/<token>
where <token> == that account's `custom_webhook_token`. nginx rewrites the trailing
segment to `?token=` (see nginx/frappe.conf.template); the token both authenticates the
caller and identifies the receiving account (routing.account_by_token) — inbound never
depends on a WATI payload field. Setup: vault runbook 02-operations/runbooks/09.
"""
import frappe
from frappe.rate_limiter import rate_limit

from tatva_connect.whatsapp import adapter
from tatva_connect.whatsapp import api as wati
from tatva_connect.whatsapp import routing
from tatva_connect.webhooks import spine


@frappe.whitelist(allow_guest=True)
@rate_limit(key="token", limit=600, seconds=60, ip_based=True)
def webhook(**kwargs):
	"""Fast-ack endpoint. The spine does kill-switch -> token auth+scope -> always-on raw
	log -> relevance pre-filter -> enqueue, and returns 'ok' fast; the worker dedupes and
	hands to the WATI adapter. Rate-limited per source IP (WATI is a shared-tenant
	firehose) to bound abusive bursts; a real WATI burst stays well under the cap."""
	return spine.receive(
		"WATI",
		enabled=wati.is_enabled,
		resolve_account=routing.account_by_token,
		adapter=adapter,
	)


def process_event(payload: dict, account=None):
	"""One-brain ingest entry reused by the WATI history backfill (backfill.py): hand to
	the WATI adapter — the SAME eventType dispatch + routing / insert / status / media
	logic the live webhook worker takes, so live + backfill share one brain. Behaviour is
	identical to the old in-module dispatch (per-helper dedupe unchanged). Runs privileged
	(the caller already gates access)."""
	if frappe.session.user == "Guest":
		frappe.set_user("Administrator")
	adapter.handle(payload, None, account)


@frappe.whitelist()
def webhook_urls():
	"""Back-compat WATI-only helper: {account: url|None} across ALL WhatsApp Accounts
	(None = token not set yet). The per-account form now uses the provider-neutral
	`tatva_connect.webhooks.urls.get_account_webhook_urls`; this thin wrapper stays for any
	existing caller. System Manager only (default @frappe.whitelist gating)."""
	from tatva_connect.webhooks.urls import get_account_webhook_urls

	out = {}
	for name in frappe.get_all("WhatsApp Account", pluck="name"):
		res = get_account_webhook_urls("WhatsApp Account", name)
		out[name] = res["urls"][0] if res["urls"] else None
	return out


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
