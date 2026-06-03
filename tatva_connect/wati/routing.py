"""Route a WhatsApp Message to the correct WATI account by the lead's taxonomy.

One WATI tenant = one `WhatsApp Account`. A `WATI Account Routing` rule maps a
Product Line (CRM Vertical) / Group (CRM Group) / Program (CRM Program) to an
account. A rule matches a lead only if EVERY axis it specifies matches; among
matching rules the MOST SPECIFIC wins (Program > Group > Product Line).

There is deliberately **no global default** — an unmatched lead raises at send
time (see message.set_whatsapp_account). We never silently send through the
wrong tenant. A catch-all is possible only by an explicit rule with all axes
blank, if the operator chooses to create one.
"""
import frappe
from frappe import _

# Specificity weights — higher = more specific.
_PROGRAM_W, _GROUP_W, _VERTICAL_W = 4, 2, 1


def resolve_account_for_lead(lead):
	"""Return the WhatsApp Account name for a lead, or None if no rule matches.

	Most-specific rule wins (Program > Group > Product Line). If two equally
	specific rules point at DIFFERENT accounts for the same lead, that's an
	ambiguous config — raise rather than pick one silently.
	"""
	program = lead.get("custom_current_program")
	group = lead.get("custom_psp_group")
	vertical = lead.get("custom_vertical")

	# Only route to a WATI account that is Active. This is the per-account kill-switch:
	# set an account Inactive and its leads are blocked (never sent through a dead
	# tenant) rather than silently delivered.
	active = set(
		frappe.get_all("WhatsApp Account", filters={"custom_is_wati": 1, "status": "Active"}, pluck="name")
	)

	best, best_score, tie = None, -1, False
	for rule in frappe.get_all(
		"WATI Account Routing",
		fields=["whatsapp_account", "program", "psp_group", "vertical"],
	):
		if rule.whatsapp_account not in active:
			continue
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
			best, best_score, tie = rule.whatsapp_account, score, False
		elif score == best_score and rule.whatsapp_account != best:
			tie = True
	if tie:
		frappe.throw(
			_(
				"Ambiguous WATI routing: two equally-specific rules point at different "
				"accounts for this lead. Fix the WATI Account Routing rules."
			),
			title=_("Ambiguous WATI route"),
		)
	return best


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


def account_for_channel(channel_number, account_hint=None):
	"""Inbound: resolve which WhatsApp Account received the message.

	Precedence:
	  1. account_hint — the account encoded in the per-tenant webhook URL
	     (operator-controlled, doesn't depend on any WATI payload field);
	  2. the WABA channel number the message arrived on;
	  3. single-tenant safety net — only if EXACTLY ONE WATI account exists.

	With 2+ WATI accounts and no hint/channel match we return None (the caller
	logs + still stores the message) rather than guess and misattribute it to the
	wrong tenant.
	"""
	from tatva_connect.wati import api as wati

	if account_hint and frappe.db.exists(
		"WhatsApp Account", {"name": account_hint, "custom_is_wati": 1}
	):
		return account_hint

	if channel_number:
		digits = wati.normalize_number(channel_number)
		account = frappe.db.get_value(
			"WhatsApp Account", {"custom_wati_channel_number": digits}, "name"
		)
		if account:
			return account

	accounts = frappe.get_all("WhatsApp Account", filters={"custom_is_wati": 1}, pluck="name")
	return accounts[0] if len(accounts) == 1 else None


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
