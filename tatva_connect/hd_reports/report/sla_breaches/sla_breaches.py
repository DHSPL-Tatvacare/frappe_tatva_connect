# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Tickets that missed their first-response or resolution target, judged by Helpdesk's own SLA methods, one row per missed target."""
import frappe
from frappe import _

from tatva_connect.helpdesk import TICKET

SLA = "HD Service Level Agreement"
FIELDS = ["name", "subject", "agent_group", "ticket_type", "priority", "sla", "response_by", "first_responded_on",
          "resolution_by", "resolution_date", "on_hold_since"]


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.only_for(("System Manager", "Agent Manager"))
	where = {"sla": ["is", "set"], "opening_date": ["between", [filters.from_date, filters.to_date]]}
	for key, field in (("team", "agent_group"), ("ticket_type", "ticket_type"), ("priority", "priority")):
		if filters.get(key):
			where[field] = filters.get(key)
	tickets = frappe.get_list(TICKET, filters=where, fields=FIELDS, order_by="opening_date desc, name desc")
	rows = []
	for t in tickets:
		sla = frappe.get_cached_doc(SLA, t.sla)  # authz-ok: tier-b — only_for gate above; the SLA is read only to judge a ticket get_list already let the viewer see
		# A paused, unanswered ticket has no response target, which Helpdesk's test cannot compare: no target is no miss.
		if t.response_by and sla.is_first_response_failed(t):
			rows.append(_row(t, sla, _("First Response"), t.response_by, t.first_responded_on))
		if sla.is_resolution_failed(t):
			rows.append(_row(t, sla, _("Resolution"), t.resolution_by, t.resolution_date))
	return _columns(), rows


def _row(t, sla, missed, due, actual):
	"""Overdue by is Helpdesk's own failed-by: working seconds from the target to the reply, or to now while still waiting."""
	return {
		"ticket": t.name, "subject": t.subject, "team": t.agent_group, "ticket_type": t.ticket_type, "priority": t.priority,
		"missed": missed, "due": due, "actual": actual,
		"overdue_by": sla.calc_elapsed_time(due, actual or sla.get_active_ticket_time(t)),
	}


def _columns():
	return [
		{"label": _("Ticket"), "fieldname": "ticket", "fieldtype": "Link", "options": TICKET, "width": 90},
		{"label": _("Subject"), "fieldname": "subject", "fieldtype": "Data", "width": 260},
		{"label": _("Team"), "fieldname": "team", "fieldtype": "Link", "options": "HD Team", "width": 140},
		{"label": _("Type"), "fieldname": "ticket_type", "fieldtype": "Link", "options": "HD Ticket Type", "width": 120},
		{"label": _("Priority"), "fieldname": "priority", "fieldtype": "Link", "options": "HD Ticket Priority", "width": 100},
		{"label": _("Missed"), "fieldname": "missed", "fieldtype": "Data", "width": 120},
		{"label": _("Due"), "fieldname": "due", "fieldtype": "Datetime", "width": 160},
		{"label": _("Actual"), "fieldname": "actual", "fieldtype": "Datetime", "width": 160},
		{"label": _("Overdue By"), "fieldname": "overdue_by", "fieldtype": "Duration", "width": 130},
	]
