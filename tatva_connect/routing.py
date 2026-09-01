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

import frappe
from frappe import _

# Specificity weights — higher = more specific.
_PROGRAM_W, _GROUP_W, _VERTICAL_W = 4, 2, 1


def _specificity(rule) -> int:
	"""How tightly a rule is scoped. Read forwards to pick the best match for a lead and backwards to
	pick an account's broadest catchment, so it is named once — two copies of a weighting are two things
	to keep in step, and the weights being powers of two is load-bearing: a score names exactly WHICH
	axes a rule sets, which is why two rules matching one lead at one score must be the same triple.
	"""
	return (
		(_PROGRAM_W if rule.program else 0)
		+ (_GROUP_W if rule.psp_group else 0)
		+ (_VERTICAL_W if rule.vertical else 0)
	)


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
		score = _specificity(rule)
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
			# one lead's ambiguous/raising routing must not block the others — but log it so a
			# genuine misconfig (e.g. two equally-specific rules) surfaces instead of vanishing
			frappe.log_error(title="routing: lead account resolve failed", message=frappe.get_traceback())
			continue
	return out


def grain_for_account(account, *, routing_doctype, account_link_field):
	"""The grain a lead must carry to route BACK to `account` — `resolve_account_for_lead` read backwards.

	A lead born from an inbound message has no grain of its own to resolve, so one has to be chosen for
	it. Choosing it is not a second decision: the routing rules already say which grain reaches this
	account, and reading them is what makes "the lead routes back to the number it wrote to" true by
	construction rather than by an operator typing the same three values into a second place. A grain
	configured beside the rules can disagree with them, and a lead created at a grain that resolves
	elsewhere is an orphan — its own message cannot attach to it.

	The LEAST specific rule wins, the mirror of resolution's most-specific: a rule that pins a programme
	describes one corner of the account's traffic, while the broadest rule is the account's whole
	catchment, which is the only honest thing to say about a stranger whose programme nobody knows yet.

	Returns `(vertical, group, program)` with `""` for an axis the rule leaves open, or `None` when the
	account has no rule or two equally-broad rules disagree — fail-closed, because inventing a grain is
	exactly the guess this reads the rules to avoid.
	"""
	scored = []
	for rule in frappe.get_all(
		routing_doctype,
		filters={account_link_field: account},
		fields=["program", "psp_group", "vertical"],
	):
		scored.append((_specificity(rule), (rule.vertical or "", rule.psp_group or "", rule.program or "")))
	if not scored:
		return None
	broadest = min(score for score, _ in scored)
	grains = {grain for score, grain in scored if score == broadest}
	return grains.pop() if len(grains) == 1 else None
