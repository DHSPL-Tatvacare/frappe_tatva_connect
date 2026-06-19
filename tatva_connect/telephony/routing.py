"""Route a telephony call to the correct Acefone account by taxonomy / DID.

One Acefone tenant = one `CRM Telephony Account` (its own DID, agent line and API
token). A `CRM Telephony Routing` rule maps a Product Line (CRM Vertical) /
Group (CRM Group) / Program (CRM Program) to an account. A rule matches a lead
only if EVERY axis it specifies matches; among matching rules the MOST SPECIFIC
wins (Program > Group > Product Line).

There is deliberately **no global default** — an unmatched record returns None
and the caller (adapter.make_acefone_call) raises rather than dial through the
wrong tenant.

This module is the Acefone-flavoured **thin wrapper** over the shared engine in
`tatva_connect.routing`: it declares Acefone's config (account doctype, token
field, routing doctype, link field, active-account rule) and owns the Acefone
phone query (last-10 LIKE). All resolution logic lives in the shared engine.
"""
import frappe

from tatva_connect import routing as engine

_ROUTING_DOCTYPE = "CRM Telephony Routing"
_ACCOUNT_LINK_FIELD = "telephony_account"


def _active_account_names():
	"""Selectable Acefone accounts: enabled==1. The per-account kill-switch — disable
	an account and its rules become unselectable (the lead is blocked, never dialled
	through a dead tenant) rather than silently routed. Mirrors WATI's Active-only
	filter."""
	return set(
		frappe.get_all("CRM Telephony Account", filters={"enabled": 1}, pluck="name")
	)


def resolve_account_for_lead(lead):
	"""Return the Acefone Account name for a lead, or None if no rule matches.

	Most-specific rule wins (Program > Group > Product Line); an ambiguous
	equally-specific tie raises. Thin wrapper over the shared engine."""
	return engine.resolve_account_for_lead(
		lead,
		routing_doctype=_ROUTING_DOCTYPE,
		account_link_field=_ACCOUNT_LINK_FIELD,
		active_names=_active_account_names(),
	)


def leads_for_number_and_account(lead_names, account):
	"""Inbound attribution: of the candidate leads sharing a phone, return those
	whose taxonomy routes to `account`. The inverse of resolve_account_for_lead — it
	scopes an inbound call to exactly the leads on the receiving account's line, never
	across accounts. Returns [] if account is falsy. Thin wrapper over the shared engine.

	`lead_names` are already-anchored candidates (same last-10 phone) — the Acefone
	phone-match seam lives in the adapter."""
	return engine.leads_for_number_and_account(
		lead_names,
		account,
		routing_doctype=_ROUTING_DOCTYPE,
		account_link_field=_ACCOUNT_LINK_FIELD,
		active_names=_active_account_names(),
	)


def resolve_for_reference(reference_doctype, reference_name):
	"""Resolve the Acefone Account for an outbound call from a CRM record.

	* CRM Lead  -> resolve by the lead's own taxonomy.
	* CRM Deal  -> resolve via the deal's linked lead if it carries the taxonomy
	  (Deals don't always have the custom_* fields directly); else None.
	Defensive: any missing field / lookup failure returns None rather than raise.
	"""
	if not reference_doctype or not reference_name:
		return None

	if reference_doctype == "CRM Lead":
		lead = frappe.get_cached_doc("CRM Lead", reference_name)
		return resolve_account_for_lead(lead)

	if reference_doctype == "CRM Deal":
		deal = frappe.get_cached_doc("CRM Deal", reference_name)
		# A Deal may carry the taxonomy directly, or via a linked lead.
		if deal.get("custom_vertical") or deal.get("custom_group") or deal.get(
			"custom_current_program"
		):
			return resolve_account_for_lead(deal)
		lead_name = deal.get("lead") or deal.get("custom_lead")
		if lead_name and frappe.db.exists("CRM Lead", lead_name):
			lead = frappe.get_cached_doc("CRM Lead", lead_name)
			return resolve_account_for_lead(lead)

	return None


def account_by_webhook_token(token):
	"""Inbound auth + identity in ONE lookup: the Telephony Account whose `webhook_token`
	matches. Each tenant registers its webhook URLs carrying its own token
	(/webhooks/telephony/<provider>/<token>/<event>), so the token both authenticates the
	caller and names the receiving account — no dependence on the CDR's did_number for auth.
	Fail-closed on ambiguity (two accounts sharing a token -> None, not a best guess).
	Thin wrapper over the shared engine."""
	return engine.account_by_token("CRM Telephony Account", "webhook_token", token)


def account_for_did(did_number):
	"""Inbound: resolve which Acefone Account owns the DID a call landed on.

	Matches an Acefone Account whose `caller_id` digits equal the CDR's
	`did_number` digits (last-10 LIKE). With
	2+ accounts and no DID match we return None rather than guess and misattribute
	to the wrong tenant — there is deliberately no single-account fallback.
	"""
	from tatva_connect.telephony import api as acefone

	digits = acefone.normalize_number(did_number)
	if not digits:
		return None
	account = frappe.db.get_value(
		"CRM Telephony Account", {"caller_id": ["like", f"%{digits[-10:]}%"]}, "name"
	)
	return account or None
