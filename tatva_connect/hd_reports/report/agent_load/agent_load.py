# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Per active helpdesk agent: their teams, the open tickets they hold, what they resolved in the window, and their check-in now."""
import frappe
from frappe import _
from frappe.utils import add_days

from tatva_connect.helpdesk import TICKET
from tatva_connect.lead.checkin import statuses as checkin_statuses
from tatva_connect.lead.report.rep_load.rep_load import counts as rep_counts

TEAM, MEMBER = "HD Team", "HD Team Member"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.only_for(("System Manager", "Agent Manager"))
	agents = frappe.get_all("HD Agent", filters={"is_active": 1}, fields=["user", "agent_name"])  # authz-ok: tier-b — only_for gate above; agent names only
	teams = _teams([a.user for a in agents])
	if filters.team:
		agents = [a for a in agents if filters.team in teams.get(a.user, ())]
	users = [a.user for a in agents]
	if not users:
		return _columns(), []
	held = _counts({"allocated_to": ["in", users], "status": "Open"}, [["status_category", "!=", "Resolved"]])
	resolved = _counts({"allocated_to": ["in", users], "status": ["!=", "Cancelled"]},
	                   [["resolution_date", ">=", filters.from_date], ["resolution_date", "<", add_days(filters.to_date, 1)]])
	checkin = checkin_statuses(users)
	rows = [{
		"agent": a.user, "agent_name": a.agent_name, "teams": ", ".join(sorted(teams.get(a.user, ()))),
		"open_tickets": held.get(a.user, 0), "resolved": resolved.get(a.user, 0), "checkin": checkin.get(a.user) or _("never"),
	} for a in agents]
	rows.sort(key=lambda r: (-r["open_tickets"], -r["resolved"], r["agent"]))
	return _columns(), rows


def _teams(users):
	"""`{user: {team}}` over enabled teams, from Helpdesk's own team member rows."""
	enabled = frappe.get_all(TEAM, filters={"disabled": 0}, pluck="name")  # authz-ok: tier-b — only_for gate above; team names only
	members = frappe.get_all(MEMBER, filters={"parenttype": TEAM, "parent": ["in", enabled or [""]], "user": ["in", users]},  # authz-ok: tier-b — only_for gate above
	                         fields=["parent", "user"])
	out = {}
	for m in members:
		out.setdefault(m.user, set()).add(m.parent)
	return out


def _counts(todo_filters, ticket_filters):
	"""ToDo rows per agent on tickets the viewer can read, through Rep Load's one counter."""
	return rep_counts("ToDo", "allocated_to", {**todo_filters, "reference_type": TICKET}, "reference_name", TICKET, ticket_filters)


def _columns():
	return [
		{"label": _("Agent"), "fieldname": "agent", "fieldtype": "Link", "options": "User", "width": 240},
		{"label": _("Name"), "fieldname": "agent_name", "fieldtype": "Data", "width": 160},
		{"label": _("Teams"), "fieldname": "teams", "fieldtype": "Data", "width": 200},
		{"label": _("Open Tickets"), "fieldname": "open_tickets", "fieldtype": "Int", "width": 120},
		{"label": _("Resolved"), "fieldname": "resolved", "fieldtype": "Int", "width": 100},
		{"label": _("Check-in Now"), "fieldname": "checkin", "fieldtype": "Data", "width": 130},
	]
