"""Route a telephony call to the correct Acefone account by taxonomy / DID.

One Acefone tenant = one `Acefone Account` (its own DID, agent line and API
token). An `Acefone Account Routing` rule maps a Product Line (CRM Vertical) /
Group (CRM Group) / Program (CRM Program) to an account. A rule matches a lead
only if EVERY axis it specifies matches; among matching rules the MOST SPECIFIC
wins (Program > Group > Product Line).

There is deliberately **no global default** — an unmatched record returns None
and the caller (handler.make_acefone_call) raises rather than dial through the
wrong tenant. Mirrors tatva_connect/wati/routing.py.
"""
import frappe
from frappe import _

# Specificity weights — higher = more specific.
_PROGRAM_W, _GROUP_W, _VERTICAL_W = 4, 2, 1


def resolve_account_for_lead(lead):
	"""Return the Acefone Account name for a lead, or None if no rule matches.

	Most-specific rule wins (Program > Group > Product Line). If two equally
	specific rules point at DIFFERENT accounts for the same lead, that's an
	ambiguous config — raise rather than pick one silently.
	"""
	program = lead.get("custom_current_program")
	group = lead.get("custom_psp_group")
	vertical = lead.get("custom_vertical")

	best, best_score, tie = None, -1, False
	for rule in frappe.get_all(
		"Acefone Account Routing",
		fields=["acefone_account", "program", "psp_group", "vertical"],
	):
		# Every axis the rule specifies must match the lead.
		if rule.program and rule.program != program:
			continue
		if rule.psp_group and rule.psp_group != group:
			continue
		if rule.vertical and rule.vertical != vertical:
			continue
		score = (
			(_PROGRAM_W if rule.program else 0)
			+ (_GROUP_W if rule.psp_group else 0)
			+ (_VERTICAL_W if rule.vertical else 0)
		)
		if score > best_score:
			best, best_score, tie = rule.acefone_account, score, False
		elif score == best_score and rule.acefone_account != best:
			tie = True
	if tie:
		frappe.throw(
			_(
				"Ambiguous Acefone routing: two equally-specific rules point at different "
				"accounts for this record. Fix the Acefone Account Routing rules."
			),
			title=_("Ambiguous Acefone route"),
		)
	return best


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
		if deal.get("custom_vertical") or deal.get("custom_psp_group") or deal.get(
			"custom_current_program"
		):
			return resolve_account_for_lead(deal)
		lead_name = deal.get("lead") or deal.get("custom_lead")
		if lead_name and frappe.db.exists("CRM Lead", lead_name):
			lead = frappe.get_cached_doc("CRM Lead", lead_name)
			return resolve_account_for_lead(lead)

	return None


def account_for_did(did_number):
	"""Inbound: resolve which Acefone Account owns the DID a call landed on.

	Matches an Acefone Account whose `caller_id` digits equal the CDR's
	`did_number` digits (last-10 LIKE, mirroring wati.account_for_channel). With
	2+ accounts and no DID match we return None rather than guess and misattribute
	to the wrong tenant — there is deliberately no single-account fallback.
	"""
	from tatva_connect.acefone import api as acefone

	digits = acefone.normalize_number(did_number)
	if not digits:
		return None
	account = frappe.db.get_value(
		"Acefone Account", {"caller_id": ["like", f"%{digits[-10:]}%"]}, "name"
	)
	return account or None
