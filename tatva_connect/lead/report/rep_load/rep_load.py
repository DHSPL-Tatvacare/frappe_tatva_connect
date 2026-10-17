# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Per rep: leads given to them in the window, and what they hold now, so an overloaded or starved rep shows at a glance."""
import frappe
from crm.permissions.org_hierarchy import _in_hierarchy, _team_mem_query, hierarchy_enabled
from frappe import _
from frappe.utils import add_days, now_datetime

from tatva_connect.lead.checkin import statuses as checkin_statuses
from tatva_connect.tasks.tasks import open_statuses

LEAD = "CRM Lead"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.only_for(("System Manager", "Sales Manager"))
	team = _team(frappe.session.user)
	window = [["reference_type", "=", LEAD], ["creation", ">=", filters.from_date], ["creation", "<", add_days(filters.to_date, 1)]]
	if team is not None:
		window.append(["allocated_to", "in", team])
	given = counts("ToDo", "allocated_to", window, "reference_name")
	reps = sorted(set(given) | set(team or ()))
	if not reps:
		return _columns(), []
	open_leads = counts("ToDo", "allocated_to", {"allocated_to": ["in", reps], "reference_type": LEAD, "status": "Open"}, "reference_name")
	open_tasks = counts("CRM Task", "assigned_to", {"assigned_to": ["in", reps], "reference_doctype": LEAD, "status": ["in", open_statuses()]}, "reference_docname")
	overdue = counts("CRM Task", "assigned_to", {"assigned_to": ["in", reps], "reference_doctype": LEAD, "status": ["in", open_statuses()], "due_date": ["<", now_datetime()]},
	                  "reference_docname")
	checkin = checkin_statuses(reps)
	rows = [{
		"rep": rep, "given": given.get(rep, 0), "open_leads": open_leads.get(rep, 0), "open_tasks": open_tasks.get(rep, 0),
		"overdue": overdue.get(rep, 0), "checkin": checkin.get(rep) or _("never"),
	} for rep in reps]
	rows.sort(key=lambda r: (-r["given"], -r["open_leads"]))
	return _columns(), rows


def _team(user):
	"""None for a viewer who sees every rep; their hierarchy subtree for a manager inside it."""
	if "System Manager" in frappe.get_roles(user) or not (hierarchy_enabled() and _in_hierarchy(user)):
		return None
	return _team_mem_query(user).run(pluck=True)


def counts(doctype, by, filters, ref_column, record=LEAD, record_filters=None):
	"""Rows per person, counted only on records the viewer can read (leads by default): the list's own permission query, never a second matcher."""
	readable = frappe.qb.get_query(record, fields=["name"], filters=record_filters, ignore_permissions=False)
	query = frappe.qb.get_query(doctype, filters=filters, fields=[by, {"COUNT": "*", "as": "n"}], group_by=by)
	rows = query.where(frappe.qb.DocType(doctype)[ref_column].isin(readable)).run(as_dict=True)
	return {r[by]: r.n for r in rows}


def _columns():
	return [
		{"label": _("Rep"), "fieldname": "rep", "fieldtype": "Link", "options": "User", "width": 240},
		{"label": _("Leads Given"), "fieldname": "given", "fieldtype": "Int", "width": 110},
		{"label": _("Open Leads"), "fieldname": "open_leads", "fieldtype": "Int", "width": 110},
		{"label": _("Open Tasks"), "fieldname": "open_tasks", "fieldtype": "Int", "width": 110},
		{"label": _("Overdue Tasks"), "fieldname": "overdue", "fieldtype": "Int", "width": 120},
		{"label": _("Check-in Now"), "fieldname": "checkin", "fieldtype": "Data", "width": 130},
	]
