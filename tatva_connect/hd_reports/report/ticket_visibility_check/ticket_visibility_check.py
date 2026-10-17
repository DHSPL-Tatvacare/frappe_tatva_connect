# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Can one user open one ticket, does it show in their list, and which rule decides: the verdict is frappe's own, each reason is one layer frappe or helpdesk applies."""
import frappe
from frappe import _
from frappe.permissions import get_role_permissions, has_controller_permissions, has_user_permission
from frappe.share import get_shared
from helpdesk.helpdesk.doctype.hd_ticket.hd_ticket import _is_customer_manager
from helpdesk.utils import get_agents_team, is_agent

from tatva_connect.helpdesk import SETTINGS, TICKET


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(TICKET, "read", filters.ticket, throw=True)
	user = filters.user
	doc = frappe.get_doc(TICKET, filters.ticket)
	raisers = [f"{df}: {doc.get(df)}" for df in ("contact", "raised_by", "owner") if doc.get(df) == user]
	assigned = frappe.parse_json(doc._assign) or []
	teams = get_agents_team(user)
	team_names = [t.team_name for t in teams]
	unrestricted = [t.team_name for t in teams if t.ignore_restrictions]
	restricted = frappe.db.get_single_value(SETTINGS, "restrict_tickets_by_agent_group")
	shared = get_shared(TICKET, user, rights=["read"], filters=[["share_name", "=", doc.name]], limit=1)

	rows = [
		_row(_("Verdict"), _("Can open the ticket"), frappe.has_permission(TICKET, "read", doc.name, user=user), _("frappe's own permission check for this user")),
		_row(_("Verdict"), _("Shows in their Tickets list"), *_listed(doc.name, user)),
		_row(_("Frappe"), _("Their roles grant ticket read"), get_role_permissions(TICKET, user=user).get("read"), ", ".join(frappe.get_roles(user)) or _("no roles")),
		_row(_("Frappe"), _("Passes their User Permissions"), has_user_permission(doc, user=user), _("frappe's User Permission check on this ticket's links")),
		_row(_("Frappe"), _("Shared with them"), bool(shared), _("a share grants read without a role") if shared else "-"),
		_row(_("Frappe"), _("Helpdesk's ticket rule admits them"), has_controller_permissions(doc, "read", user=user), _("every has_permission hook on HD Ticket")),
		_row(_("Helpdesk"), _("Raised it"), bool(raisers), ", ".join(raisers) or _("not the contact, requester or owner")),
		_row(_("Helpdesk"), _("Manages the ticket's customer"), _is_customer_manager(doc.customer, user), doc.customer or _("no customer")),
		_row(_("Helpdesk"), _("Is an active agent"), is_agent(user), _("an agent passes the team rule below; anyone else needs a rule above")),
		_row(_("Helpdesk"), _("Tickets restricted by team"), restricted, _("HD Settings: on, an agent sees only the tickets below") if restricted else _("HD Settings: off, every agent sees every ticket")),
		_row(_("Helpdesk"), _("Assigned to them"), user in assigned, ", ".join(assigned) or _("no assignment")),
		_row(_("Helpdesk"), _("On the ticket's team"), doc.agent_group in team_names, _("team {0}; theirs: {1}").format(doc.agent_group or _("none"), ", ".join(team_names) or _("none"))),
		_row(_("Helpdesk"), _("On a team that sees every team"), bool(unrestricted), ", ".join(unrestricted) or "-"),
	]
	columns = [
		{"label": _("Check"), "fieldname": "kind", "fieldtype": "Data", "width": 110},
		{"label": _("Question"), "fieldname": "question", "fieldtype": "Data", "width": 260},
		{"label": _("Answer"), "fieldname": "answer", "fieldtype": "Data", "width": 90},
		{"label": _("Why"), "fieldname": "why", "fieldtype": "Data", "width": 460},
	]
	return columns, rows


def _listed(ticket, user):
	try:
		return bool(frappe.get_list(TICKET, filters={"name": ticket}, user=user, pluck="name", limit=1)), _("the same list query their Tickets page runs")
	except frappe.PermissionError:  # surfaced as the answer: frappe refuses this user the list itself
		return False, _("frappe refuses them the Tickets list itself")


def _row(kind, question, yes, why):
	return {"kind": kind, "question": question, "answer": _("Yes") if yes else _("No"), "why": why}
