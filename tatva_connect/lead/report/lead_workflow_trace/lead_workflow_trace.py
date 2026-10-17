# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Every workflow journey one lead went through, newest first, and each step it took in order with its outcome."""
import frappe
from frappe import _

LEAD = "CRM Lead"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(LEAD, "read", filters.lead, throw=True)
	journeys = frappe.get_all("CRM Workflow Journey", filters={"subject_doctype": LEAD, "subject_name": filters.lead},  # authz-ok: tier-b — frappe.has_permission on the lead above is the gate
		fields=["name", "creation", "workflow", "status", "stop_reason", "current_node"], order_by="creation desc")
	steps = {}
	if journeys:
		for s in frappe.get_all("CRM Workflow Step Log", filters={"journey": ["in", [j.name for j in journeys]]},  # authz-ok: tier-b — gated above
				fields=["journey", "creation", "node_id", "node_type", "outcome", "detail"], order_by="creation asc, name asc"):
			steps.setdefault(s.journey, []).append(s)
	rows = []
	for j in journeys:
		rows.append({"when": j.creation, "workflow": j.workflow, "step": _("Journey"), "kind": "", "outcome": j.status,
			"detail": j.stop_reason or (_("now at {0}").format(j.current_node) if j.current_node else ""), "journey": j.name, "indent": 0})
		for s in steps.get(j.name, []):
			rows.append({"when": s.creation, "workflow": j.workflow, "step": s.node_id, "kind": s.node_type, "outcome": s.outcome,
				"detail": s.detail, "journey": j.name, "indent": 1})
	columns = [
		{"label": _("When"), "fieldname": "when", "fieldtype": "Datetime", "width": 160},
		{"label": _("Workflow"), "fieldname": "workflow", "fieldtype": "Link", "options": "CRM Workflow", "width": 200},
		{"label": _("Step"), "fieldname": "step", "fieldtype": "Data", "width": 180},
		{"label": _("Kind"), "fieldname": "kind", "fieldtype": "Data", "width": 130},
		{"label": _("Outcome"), "fieldname": "outcome", "fieldtype": "Data", "width": 110},
		{"label": _("Detail"), "fieldname": "detail", "fieldtype": "Data", "width": 320},
		{"label": _("Journey"), "fieldname": "journey", "fieldtype": "Link", "options": "CRM Workflow Journey", "width": 120},
	]
	return columns, rows
