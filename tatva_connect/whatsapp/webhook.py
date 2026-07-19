"""The WhatsApp inbound endpoint — thin, on the shared ingress spine.

A provider POSTs its ENTIRE tenant's traffic here (a shared-tenant firehose), so the endpoint must be
cheap and safe. ALL of that — kill-switch, token auth + account scoping, always-on raw log, cheap
relevance pre-filter, fast 2xx ACK + enqueue, and the dedupe-then-handle worker — lives in ONE place:
the spine (`tatva_connect.webhooks.spine`). The parsing lives in the account's adapter, and the
persistence in `tatva_connect.whatsapp.ingest`.

This module keeps only the surface the rest of the app references:
  * webhook()               — the guest endpoint; delegates to spine.receive("whatsapp", …)
  * webhook_urls()          — admin helper: the URL to register on each provider dashboard
  * pin_inbound_reference() — before_save hook on WhatsApp Message (wired in hooks.py)

Register on each provider dashboard:
    https://<host>/webhooks/whatsapp/<token>
where <token> is that account's `custom_webhook_token`. nginx rewrites the trailing segment to
`?token=` (see nginx/frappe.conf.template); the token both authenticates the caller and identifies the
receiving account (webhooks.ingress.verify), and the account names its own provider — so inbound
depends on no payload field and the URL survives a change of vendor. Setup: vault runbook
02-operations/runbooks/09.
"""
import frappe
from frappe.rate_limiter import rate_limit

from tatva_connect.webhooks import ingress, spine
from tatva_connect.whatsapp import channel, roles

# Per-minute cap per caller IP, tunable in CRM WhatsApp Settings.
_rate_limit = ingress.rate_limit_for("CRM WhatsApp Settings", 600)


@frappe.whitelist(allow_guest=True)  # guest-ok: WhatsApp webhook, no session — spine verifies a shared token before acting (A.16)
@rate_limit(limit=_rate_limit, seconds=60, ip_based=True)
def webhook(**_kwargs):
	"""Fast-ack endpoint. The spine does kill-switch -> token auth+scope -> always-on raw log ->
	relevance pre-filter -> enqueue, and returns 'ok' fast; the worker dedupes and hands to the
	account's adapter. Rate-limited per source IP (a shared-tenant firehose) to bound abusive bursts; a
	real burst stays well under the cap."""
	return spine.receive("whatsapp", enabled=channel.is_enabled)


@frappe.whitelist()
def webhook_urls():
	"""{account: url|None} across ALL WhatsApp Accounts (None = token not set yet). The per-account
	form uses the channel-neutral `tatva_connect.webhooks.urls.get_account_webhook_urls`; this is the
	all-accounts sweep. WhatsApp Admin / System Manager only — these are account config URLs."""
	frappe.only_for(roles.ADMIN_ROLES)  # config endpoint: a bare @frappe.whitelist() allows ANY logged-in user, so gate explicitly
	from tatva_connect.webhooks.urls import get_account_webhook_urls

	out = {}
	for name in frappe.get_all("WhatsApp Account", pluck="name"):  # authz-ok: gated to Admin above; enumerates config accounts, not user records
		res = get_account_webhook_urls("WhatsApp Account", name)
		out[name] = res["urls"][0] if res["urls"] else None
	return out


def pin_inbound_reference(doc, method=None):
	"""before_save: restore the account-matched lead that crm.api.whatsapp.validate overwrote with
	first-lead-by-phone. crm registers an unconditional `validate` doc_event on WhatsApp Message that
	rewrites reference_name from the phone; Frappe runs validate BEFORE before_save, so re-pinning here
	wins — and it runs before the row is written and before crm's on_update realtime, so both use the
	right lead.

	Gated on the FLAG, and on nothing else. It used to re-check the messaging kill-switch, which read as
	caution and was a data-loss bug: the ingest that stamps this flag has ALREADY passed that switch at
	the front door, so the second check could only ever be false during a backfill — and then crm's
	clobber stood, both rows on a shared number were pinned to the same lead, and the second insert died
	on the (message_id, reference_name) unique index. The lead sharing the number got nothing and the
	whole event was lost in the worker. The flag is set by our own gated ingest and by nothing else, so
	it IS the gate; this handler is inert for every other row.
	"""
	pinned = doc.flags.get("tatva_pinned_lead")
	if pinned and (doc.type or "") == "Incoming":
		doc.reference_doctype = "CRM Lead"
		doc.reference_name = pinned
