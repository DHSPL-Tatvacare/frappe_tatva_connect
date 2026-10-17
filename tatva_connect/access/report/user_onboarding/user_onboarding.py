# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Is each user ready to go live on each grain they hold: the handbook's eight onboarding steps, read in a fixed number of queries for any number of users."""
from collections import defaultdict

import frappe
from crm.permissions.org_hierarchy import hierarchy_enabled
from frappe import _

from tatva_connect.access import entitlement
from tatva_connect.access.ledger import SALES_MANAGER, SALES_USER
from tatva_connect.taxonomy.grain import covers, overlaps
from tatva_connect.telephony import resolve as seats
from tatva_connect.telephony import routing as telephony_routing
from tatva_connect.whatsapp import routing as whatsapp_routing
from tatva_connect.whatsapp.roles import WHATSAPP_ADMIN, WHATSAPP_USER

DONE, MISSING, NOT_NEEDED, OWN = "Done", "Missing", "Not needed", "User's own"

# (fieldname, label, blocks) in the handbook's order; step 8 is the person's own and never blocks.
STEPS = (
	("account", "1 Account", True),
	("roles", "2 Roles", True),
	("permissions", "3 Permissions", True),
	("hierarchy", "4 Hierarchy", True),
	("pool", "5 Pool", True),
	("extension", "6 Extension", True),
	("whatsapp", "7 WhatsApp", True),
	("notifications", "8 Notifications", False),
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission("User", "read", throw=True)
	users = list(dict.fromkeys(frappe.parse_json(filters.users) if isinstance(filters.users, str) else (filters.users or [])))
	if not users:
		return _columns(), [], _("Pick one or more users to check.")
	want = (filters.vertical or "", filters.crm_group or "", filters.program or "")
	facts = _Facts(users)
	rows = [_row(facts, user, grain, any(want)) for user in users for grain in _grains(facts, user, want)]
	ready = sum(1 for row in rows if row["verdict"] == "Ready")
	summary = [
		{"value": ready, "label": _("Ready"), "indicator": "Green", "datatype": "Int"},
		{"value": len(rows) - ready, "label": _("Not ready"), "indicator": "Red" if len(rows) - ready else "Gray", "datatype": "Int"},
		{"value": len(users), "label": _("Users checked"), "indicator": "Blue", "datatype": "Int"},
	]
	return _columns(), rows, None, None, summary


class _Facts:
	"""Every source the steps read, loaded once for the whole user list; routing and seats once per distinct grain and account."""

	def __init__(self, users):
		self.profile = {u.name: u for u in frappe.get_all(  # authz-ok: tier-b — the report's roles and User read above are the gate
			"User", filters={"name": ["in", users]}, fields=["name", "enabled", "user_type", "last_login"])}
		self.roles = defaultdict(set)
		for r in frappe.get_all("Has Role", filters={"parenttype": "User", "parent": ["in", users]}, fields=["parent", "role"]):  # authz-ok: tier-b — gated above
			self.roles[r.parent].add(r.role)
		self.notify = {n.name: n.enabled for n in frappe.get_all(  # authz-ok: tier-b — gated above
			"Notification Settings", filters={"name": ["in", users]}, fields=["name", "enabled"])}
		self.rules = frappe.get_all(  # authz-ok: tier-b — gated above
			"Assignment Rule", filters={"document_type": "CRM Lead", "disabled": 0},
			fields=["name", "grain_vertical as vertical", "grain_group as `group`", "grain_program as program"])
		self.members = defaultdict(dict)
		for m in frappe.get_all(  # authz-ok: tier-b — gated above
			"Assignment Rule User", filters={"parenttype": "Assignment Rule", "user": ["in", users]}, fields=["parent", "user", "paused"]):
			self.members[m.user][m.parent] = m.paused
		self.tree_on = hierarchy_enabled()
		self.tree = frappe.get_all("CRM Sales Hierarchy", fields=["user", "lft", "rgt"]) if self.tree_on else []  # authz-ok: tier-b — gated above
		self._grains, self._telephony, self._whatsapp, self._seats = {}, {}, {}, {}

	def grains(self, user):
		"""The user's grains as their User Permissions draw them — the registry source, whatever the flag reads today."""
		if user not in self._grains:
			self._grains[user] = entitlement._grains_from_user_permission(user)
		return self._grains[user]

	def telephony(self, grain):
		if grain not in self._telephony:
			v, g, p = grain
			self._telephony[grain] = telephony_routing.resolve_account_for_lead(
				frappe._dict(custom_vertical=v, custom_group=g, custom_current_program=p))
		return self._telephony[grain]

	def whatsapp(self, grain):
		if grain not in self._whatsapp:
			self._whatsapp[grain] = whatsapp_routing.resolve_account_for_grain(*grain)
		return self._whatsapp[grain]

	def seat(self, user, account):
		if account not in self._seats:
			at = seats.moment()
			self._seats[account] = {r.parent: r.extension for r in seats.seats_for_account(account) if seats.held_at(r, at)}
		return self._seats[account].get(user)

	def line(self, user):
		"""Everyone above and below the user in the sales hierarchy, or None when they have no node."""
		node = next((n for n in self.tree if n.user == user), None)
		if not node:
			return None
		return {n.user for n in self.tree if n.user != user and ((n.lft < node.lft and n.rgt > node.rgt) or node.lft < n.lft < node.rgt)}


def _grains(facts, user, want):
	"""The asked grain, else every grain the user holds, else one empty row that says so."""
	if any(want):
		return [want]
	return sorted(facts.grains(user)) or [None]


def _as_scope(grain):
	return dict(zip(("vertical", "group", "program"), grain, strict=True))


def _row(facts, user, grain, asked):
	profile = facts.profile.get(user) or frappe._dict(name=user)
	missing, cells = [], {}

	def mark(step, state, why=None):
		cells[step] = state
		if why and state == MISSING:
			missing.append(why)

	if not profile.enabled or profile.user_type != "System User":
		mark("account", MISSING, _("the account is disabled or is not a System User"))
	else:
		mark("account", DONE)
	if facts.roles[user] & {SALES_USER, SALES_MANAGER}:
		mark("roles", DONE)
	else:
		mark("roles", MISSING, _("give Sales User or Sales Manager on the user"))

	if grain is None:
		mark("permissions", MISSING, _("no User Permission on a vertical, group or program"))
		for step in ("hierarchy", "pool", "extension", "whatsapp"):
			mark(step, NOT_NEEDED)
	else:
		_grain_steps(facts, user, grain, asked, mark)

	opted = facts.notify.get(user)
	mark("notifications", DONE if opted else OWN)
	if not profile.last_login:
		missing.append(_("has not signed in yet"))
	blocked = any(cells[step] == MISSING for step, _label, blocks in STEPS if blocks)
	return {
		"user": user, "grain": " / ".join(a or "*" for a in grain) if grain else "-",
		**cells, "verdict": "Not ready" if blocked else "Ready", "missing": "; ".join(missing),
	}


def _grain_steps(facts, user, grain, asked, mark):
	held = facts.grains(user)
	if not asked or any(covers(_as_scope(g), *grain) for g in held):
		mark("permissions", DONE)
	else:
		mark("permissions", MISSING, _("no User Permission covering {0}").format(" / ".join(a or "*" for a in grain)))

	line = facts.line(user) if facts.tree_on else None
	if not facts.tree_on:
		mark("hierarchy", MISSING, _("the sales hierarchy is switched off in CRM settings"))
	elif line is None:
		mark("hierarchy", MISSING, _("place them in the sales hierarchy"))
	elif not any(overlaps(_as_scope(g), *grain) for other in line for g in facts.grains(other)):
		mark("hierarchy", MISSING, _("nobody above or below them in the hierarchy is on this grain"))
	else:
		mark("hierarchy", DONE)

	rules = [r.name for r in facts.rules if overlaps(r, *grain)]
	member = facts.members[user]
	if not rules:
		mark("pool", NOT_NEEDED)
	elif any(rule in member and not member[rule] for rule in rules):
		mark("pool", DONE)
	else:
		mark("pool", MISSING, _("add them to an assignment rule on this grain ({0} cover it)").format(len(rules)))

	account = facts.telephony(grain)
	if not account:
		mark("extension", NOT_NEEDED)
	elif facts.seat(user, account):
		mark("extension", DONE)
	else:
		mark("extension", MISSING, _("no extension on {0} under Telephony Agents").format(account))

	if not facts.whatsapp(grain):
		mark("whatsapp", NOT_NEEDED)
	elif facts.roles[user] & {WHATSAPP_USER, WHATSAPP_ADMIN}:
		mark("whatsapp", DONE)
	else:
		mark("whatsapp", MISSING, _("give the WhatsApp User role"))


def _columns():
	"""The User link shows the full name, so no Name column; the verdict leads the steps."""
	return [
		{"label": _("User"), "fieldname": "user", "fieldtype": "Link", "options": "User", "width": 200},
		{"label": _("Grain"), "fieldname": "grain", "fieldtype": "Data", "width": 220},
		{"label": _("Verdict"), "fieldname": "verdict", "fieldtype": "Data", "width": 110},
		*({"label": _(label), "fieldname": step, "fieldtype": "Data", "width": 100} for step, label, _blocks in STEPS),
		{"label": _("What's missing"), "fieldname": "missing", "fieldtype": "Data", "width": 420},
	]
