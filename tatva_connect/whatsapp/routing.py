"""Route a WhatsApp Message to the correct WATI account by the lead's taxonomy.

One WATI tenant = one `WhatsApp Account`. A `CRM WhatsApp Routing` rule maps a
Product Line (CRM Vertical) / Group (CRM Group) / Program (CRM Program) to an
account. A rule matches a lead only if EVERY axis it specifies matches; among
matching rules the MOST SPECIFIC wins (Program > Group > Product Line).

There is deliberately **no global default** — an unmatched lead raises at send
time (see message.set_whatsapp_account). We never silently send through the
wrong tenant, and an all-blank rule is rejected by the routing controller so a
catch-all can't be created by accident.

This module is the WATI-flavoured **thin wrapper** over the shared engine in
`tatva_connect.routing`: it declares WATI's config (account doctype, token
field, routing doctype, link field, active-account rule) and owns the WATI phone
query (exact E.164). All resolution logic lives in the shared engine.
"""
import frappe

from tatva_connect import routing as engine
from tatva_connect.whatsapp import providers

_ROUTING_DOCTYPE = "CRM WhatsApp Routing"
_ACCOUNT_LINK_FIELD = "whatsapp_account"


def _active_account_names():
	"""Selectable WATI accounts: status Active AND a registered send adapter. This is
	both the per-account kill-switch (set an account Inactive and its leads are blocked,
	never sent through a dead tenant) AND the fail-closed gate against a non-adapter
	(e.g. Meta) account: such an account is never selectable, so an unrouted lead raises
	rather than falling through to the wrong transport. Provider-neutral — any registered
	adapter qualifies."""
	return {
		a.name
		for a in frappe.get_all(
			"WhatsApp Account", filters={"status": "Active"}, fields=["name", "custom_provider"]
		)
		if a.custom_provider in providers.PROVIDERS
	}


def resolve_account_for_lead(lead):
	"""Return the WhatsApp Account name for a lead, or None if no rule matches.

	Most-specific rule wins (Program > Group > Product Line); an ambiguous
	equally-specific tie raises. Thin wrapper over the shared engine."""
	return engine.resolve_account_for_lead(
		lead,
		routing_doctype=_ROUTING_DOCTYPE,
		account_link_field=_ACCOUNT_LINK_FIELD,
		active_names=_active_account_names(),
	)


def leads_for_number_and_account(lead_names, account):
	"""Inbound attribution: of the candidate leads sharing a phone, return those whose
	routing resolves to `account`. The inverse of resolve_account_for_lead — it scopes an
	inbound message to exactly the leads sharing its conversation (phone + account), never
	across accounts. Returns [] if account is falsy. Thin wrapper over the shared engine.

	`lead_names` are already-anchored candidates — the WATI phone-match seam (exact E.164)
	lives in the adapter (see candidates_for_number)."""
	return engine.leads_for_number_and_account(
		lead_names,
		account,
		routing_doctype=_ROUTING_DOCTYPE,
		account_link_field=_ACCOUNT_LINK_FIELD,
		active_names=_active_account_names(),
	)


def candidates_for_number(number_e164):
	"""WATI phone-match seam: CRM Leads whose mobile_no is this exact E.164 (with or
	without '+'). The taxonomy filter is the shared engine — feed these to
	leads_for_number_and_account."""
	if not number_e164:
		return []
	bare = number_e164.lstrip("+")
	return frappe.get_all(
		"CRM Lead",
		filters={"mobile_no": ["in", ["+" + bare, bare]]},
		pluck="name",
	)


def resolve_for_message(msg):
	"""Resolve the account for an outgoing WhatsApp Message linked to a CRM Lead or
	CRM Deal (a Deal routes via its originating lead)."""
	dt, dn = msg.reference_doctype, msg.reference_name
	if not dn:
		return None
	if dt == "CRM Deal":
		lead_name = frappe.db.get_value("CRM Deal", dn, "lead")
		if not lead_name:
			return None
		return resolve_account_for_lead(frappe.get_cached_doc("CRM Lead", lead_name))
	if dt == "CRM Lead":
		return resolve_account_for_lead(frappe.get_cached_doc("CRM Lead", dn))
	return None


def account_by_token(token):
	"""Inbound auth + identity in ONE lookup: the WATI WhatsApp Account whose webhook
	token matches. Each tenant registers a URL carrying its own token
	(/webhooks/whatsapp/wati/<token>), so the token both authenticates the caller and
	names the receiving account — no dependence on a payload field (WATI inbound carries
	no reliable tenant id; channelPhoneNumber is present on ~13% of message events,
	tenantId on 0%). Returns the account name, or None if the token matches no WATI
	account (caller rejects). Fail-closed on ambiguity. Thin wrapper over the shared
	engine."""
	return engine.account_by_token("WhatsApp Account", "custom_webhook_token", token)


@frappe.whitelist()
def lead_has_route(reference_doctype=None, reference_name=None):
	"""Does a WATI account route to this lead? Reuses the SAME resolver used to
	send (resolve_account_for_lead) — single source of truth. The WhatsApp tab/UI
	gate calls this so it tracks routing rules automatically (no hardcoded group).

	Fail-safe: any error (incl. an ambiguous/tie config that raises) returns
	has_route=False — consistent with 'no route -> send blocked -> hide UI' — and
	is logged, so a misconfiguration surfaces in Error Log rather than silently
	showing WhatsApp on an unrouted lead.
	"""
	try:
		if reference_doctype != "CRM Lead" or not reference_name:
			return {"has_route": False, "account": None}
		lead = frappe.get_cached_doc("CRM Lead", reference_name)
		account = resolve_account_for_lead(lead)
		return {"has_route": bool(account), "account": account}
	except Exception:
		frappe.log_error(title="WATI lead_has_route failed", message=frappe.get_traceback())
		return {"has_route": False, "account": None}
