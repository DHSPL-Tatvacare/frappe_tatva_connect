# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""How each assignment of one lead was made: by a rule, a workflow step or a person, what a draw decided, and the picked rep's check-in at that moment."""
import frappe
from frappe import _

from tatva_connect.lead import checkin

LEAD = "CRM Lead"
# The workflow steps that give a lead to someone.
HAND_OFF_STEPS = ("Distribute", "Assign to User", "Route")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(LEAD, "read", filters.lead, throw=True)
	todos = frappe.get_all("ToDo", filters={"reference_type": LEAD, "reference_name": filters.lead},  # authz-ok: tier-b — frappe.has_permission on the lead above is the gate
		fields=["creation", "allocated_to", "assigned_by", "assignment_rule", "status"], order_by="creation")
	journeys = frappe.get_all("CRM Workflow Journey", filters={"subject_doctype": LEAD, "subject_name": filters.lead}, pluck="name")  # authz-ok: tier-b — gated above
	steps = frappe.get_all("CRM Workflow Step Log", filters={"journey": ["in", journeys], "node_type": ["in", HAND_OFF_STEPS]},  # authz-ok: tier-b — gated above
		fields=["creation", "node_id", "node_type", "outcome", "detail"], order_by="creation asc, name asc") if journeys else []
	rules = {r.name: r for r in frappe.get_all("Assignment Rule", filters={"name": ["in", {t.assignment_rule for t in todos if t.assignment_rule}]},  # authz-ok: tier-b — gated above
		fields=["name", "rule", "require_checkin"])} if any(t.assignment_rule for t in todos) else {}

	rows = []
	for t in todos:
		rule = rules.get(t.assignment_rule)
		how = _("{0} rule {1}").format(rule.rule, t.assignment_rule) if rule else _("by {0}").format(t.assigned_by or _("the system"))
		rows.append({"when": t.creation, "event": _("Assigned"), "person": t.allocated_to, "how": how,
			"checkin": _checkin_at(t.allocated_to, t.creation), "detail": _("assignment now {0}").format(t.status)})
	for s in steps:
		rows.append({"when": s.creation, "event": _("Workflow: {0}").format(s.node_type), "person": None, "how": s.node_id,
			"checkin": "", "detail": f"{s.outcome or ''}: {s.detail or ''}".strip(": ")})
	rows.sort(key=lambda r: r["when"])
	columns = [
		{"label": _("When"), "fieldname": "when", "fieldtype": "Datetime", "width": 160},
		{"label": _("Event"), "fieldname": "event", "fieldtype": "Data", "width": 170},
		{"label": _("Person"), "fieldname": "person", "fieldtype": "Link", "options": "User", "width": 210},
		{"label": _("How"), "fieldname": "how", "fieldtype": "Data", "width": 300},
		{"label": _("Check-in then"), "fieldname": "checkin", "fieldtype": "Data", "width": 120},
		{"label": _("Detail"), "fieldname": "detail", "fieldtype": "Data", "width": 300},
	]
	return columns, rows


def _checkin_at(user, moment):
	"""The person's check-in status at the moment, read through `checkin`; one indexed read."""
	return checkin.status_at(user, moment) or _("never checked in")
