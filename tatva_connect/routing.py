"""Provider-agnostic routing engine — the SINGLE source of truth for account
resolution, inbound token auth and lead attribution, shared by every provider
(WATI WhatsApp, Acefone telephony) on every path (send, intake, webhook,
reconcile).

Per-vendor `whatsapp/routing.py` and `telephony/routing.py` are thin wrappers
that pass their own config (account doctype, token field, routing doctype, the
account link field, and the active-account set). The only legitimate per-vendor
seams left in the adapters are the phone-match query (exact E.164 vs last-10
LIKE) and the inbound payload shapes — never the resolution logic itself.

Routing model: a routing rule maps a Product Line (CRM Vertical) / Group
(CRM Group) / Program (CRM Program) to an account. A rule matches a lead only if
EVERY axis it specifies matches; among matching rules the MOST SPECIFIC wins
(Program > Group > Product Line). There is deliberately **no global default** —
an unmatched lead resolves to None and the caller blocks rather than route
through the wrong tenant.
"""
import hmac

import frappe
from frappe import _

# Specificity weights — higher = more specific.
_PROGRAM_W, _GROUP_W, _VERTICAL_W = 4, 2, 1


def resolve_account_for_lead(lead, *, routing_doctype, account_link_field, active_names):
	"""Return the account name a lead routes to, or None if no rule matches.

	Most-specific rule wins (Program > Group > Product Line). If two equally
	specific rules point at DIFFERENT accounts for the same lead, that's an
	ambiguous config — raise rather than pick one silently.

	`routing_doctype`     — the provider's routing doctype (rows carry the link
	                        field plus program/psp_group/vertical axes).
	`account_link_field`  — the rule field naming the account (e.g.
	                        `whatsapp_account`, `telephony_account`).
	`active_names`        — the set of selectable account names (the per-vendor
	                        kill-switch / fail-closed gate); rules pointing at any
	                        account outside this set are skipped.
	"""
	program = lead.get("custom_current_program")
	group = lead.get("custom_group")
	vertical = lead.get("custom_vertical")

	best, best_score, tie = None, -1, False
	for rule in frappe.get_all(
		routing_doctype,
		fields=[account_link_field, "program", "psp_group", "vertical"],
	):
		account = rule.get(account_link_field)
		if account not in active_names:
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
			best, best_score, tie = account, score, False
		elif score == best_score and account != best:
			tie = True
	if tie:
		frappe.throw(
			_(
				"Ambiguous routing: two equally-specific rules point at different "
				"accounts for this lead. Fix the routing rules."
			),
			title=_("Ambiguous route"),
		)
	return best


def account_by_token(account_doctype, token_field, token):
	"""Inbound auth + identity in ONE lookup: the account whose webhook token
	matches `token`. Each tenant registers a URL carrying its own token, so the
	token both authenticates the caller and names the receiving account — no
	dependence on a payload field.

	The token is a Password field (encrypted store), so we read each account's via
	`get_password(..., raise_exception=False)` and compare with
	`hmac.compare_digest` — constant-time, so a wrong token can't be discovered
	byte-by-byte through timing.

	FAIL-CLOSED on ambiguity: collect ALL matches and return the account only if
	exactly one matched, else None. If two accounts somehow share a token
	(operator copy-paste), we surface the misconfiguration rather than best-guess
	one and cross-attribute a tenant's inbound traffic (invariant: no best-guess
	on attribution). A handful of rows (one per tenant), cheap on the firehose."""
	token = (token or "").strip()
	if not token:
		return None
	matches = []
	for acc in frappe.get_all(account_doctype, pluck="name"):
		stored = frappe.get_cached_doc(account_doctype, acc).get_password(
			token_field, raise_exception=False
		)
		if stored and hmac.compare_digest(str(stored), token):
			matches.append(acc)
	return matches[0] if len(matches) == 1 else None


def leads_for_number_and_account(
	candidate_lead_names, account, *, routing_doctype, account_link_field, active_names
):
	"""Inbound attribution: of the candidate leads (already phone-matched by the
	adapter), return those whose taxonomy routes to `account`. The inverse of
	resolve_account_for_lead — it scopes an inbound message/call to exactly the
	leads sharing its conversation (phone + account), never across accounts.

	Phone-matching stays in each adapter (exact E.164 vs last-10 LIKE — a
	legitimate per-vendor query); this takes the pre-fetched candidate name list.
	Returns [] if account or the candidate list is falsy."""
	if not account or not candidate_lead_names:
		return []
	out = []
	for name in candidate_lead_names:
		try:
			resolved = resolve_account_for_lead(
				frappe.get_cached_doc("CRM Lead", name),
				routing_doctype=routing_doctype,
				account_link_field=account_link_field,
				active_names=active_names,
			)
			if resolved == account:
				out.append(name)
		except Exception:
			# one lead's ambiguous/raising routing must not block the others
			continue
	return out
