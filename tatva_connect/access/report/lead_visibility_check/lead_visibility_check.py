# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Can one user open one lead, does it show in their list, and which rule decides: the verdict is frappe's own, each reason is one rule crm and the grain apply."""
import frappe
from crm.permissions.org_hierarchy import _in_hierarchy, _team_mem_query, hierarchy_enabled
from frappe import _

from tatva_connect.access import entitlement

LEAD = "CRM Lead"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(LEAD, "read", filters.lead, throw=True)
	user = filters.user
	lead = frappe.db.get_value(LEAD, filters.lead, ["lead_owner", "custom_vertical", "custom_group", "custom_current_program"], as_dict=True)
	roles = set(frappe.get_roles(user))
	in_tree = hierarchy_enabled() and _in_hierarchy(user)
	team = set(_team_mem_query(user).run(pluck=True)) if in_tree else set()
	holders = set(frappe.get_all(  # authz-ok: tier-b — frappe.has_permission on the lead above is the gate
		"ToDo", filters={"reference_type": LEAD, "reference_name": filters.lead, "status": ["!=", "Cancelled"]}, pluck="allocated_to"))
	shared = frappe.db.exists("DocShare", {"share_doctype": LEAD, "share_name": filters.lead, "user": user})
	grain = (lead.custom_vertical, lead.custom_group, lead.custom_current_program)

	rows = [
		_row(_("Verdict"), _("Can open the lead"), frappe.has_permission(LEAD, "read", filters.lead, user=user), _("frappe's own permission check for this user")),
		_row(_("Verdict"), _("Shows in their Leads list"), bool(frappe.get_list(LEAD, filters={"name": filters.lead}, user=user, pluck="name", limit=1)), _("the same list query their Leads page runs")),
		_row(_("Rule"), _("Role sees every lead"), "System Manager" in roles or ("Sales Manager" in roles and not in_tree), _("System Manager, or Sales Manager outside the sales hierarchy")),
		_row(_("Rule"), _("Lead Owner"), lead.lead_owner == user, lead.lead_owner or _("no owner")),
		_row(_("Rule"), _("Owner is in their team"), lead.lead_owner in team and lead.lead_owner != user, _team_why(lead.lead_owner, team, in_tree)),
		_row(_("Rule"), _("Assigned to them"), user in holders, ", ".join(sorted(holders)) or _("no live assignment")),
		_row(_("Rule"), _("Assigned to their team"), bool(holders & (team - {user})), ", ".join(sorted(holders & (team - {user}))) or "-"),
		_row(_("Rule"), _("Shared with them"), bool(shared), _("a share is not an assignment; see Lead Access Chain") if shared else "-"),
		_row(_("Grain"), _("Entitled to the lead's grain"), entitlement.grain_entitled(grain, user=user), " / ".join(a or "*" for a in grain)),
	]
	columns = [
		{"label": _("Check"), "fieldname": "kind", "fieldtype": "Data", "width": 110},
		{"label": _("Question"), "fieldname": "question", "fieldtype": "Data", "width": 260},
		{"label": _("Answer"), "fieldname": "answer", "fieldtype": "Data", "width": 90},
		{"label": _("Why"), "fieldname": "why", "fieldtype": "Data", "width": 460},
	]
	return columns, rows


def _team_why(owner, team, in_tree):
	if not in_tree:
		return _("they are not in the sales hierarchy")
	return _("{0} is in their hierarchy subtree").format(owner) if owner in team else _("{0} is outside their hierarchy subtree").format(owner or _("no owner"))


def _row(kind, question, yes, why):
	return {"kind": kind, "question": question, "answer": _("Yes") if yes else _("No"), "why": why}
