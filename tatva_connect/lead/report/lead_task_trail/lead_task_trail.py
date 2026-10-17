# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Every task on one lead: what created it, who holds it, and where it stands."""
import frappe
from frappe import _
from frappe.utils import cstr

from tatva_connect.tasks.tasks import completed_on

LEAD = "CRM Lead"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(LEAD, "read", filters.lead, throw=True)
	tasks = frappe.get_all("CRM Task", filters={"reference_doctype": LEAD, "reference_docname": filters.lead},  # authz-ok: tier-b — frappe.has_permission on the lead above is the gate
		fields=["name", "creation", "owner", "title", "custom_task_type", "status", "assigned_to", "due_date", "custom_completed_on", "modified",
			"custom_workflow_token", "custom_automated", "custom_lsq_task_id"], order_by="creation desc")
	rows = [{
		"created": t.creation, "task": cstr(t.name), "type": cstr(t.custom_task_type).rsplit("::", 1)[-1] or t.title,
		"status": t.status, "held_by": t.assigned_to, "created_by": t.owner, "origin": _origin(t),
		"due": t.due_date, "done_on": completed_on(t),
	} for t in tasks]
	columns = [
		{"label": _("Created"), "fieldname": "created", "fieldtype": "Datetime", "width": 160},
		{"label": _("Task"), "fieldname": "task", "fieldtype": "Link", "options": "CRM Task", "width": 90},
		{"label": _("Type"), "fieldname": "type", "fieldtype": "Data", "width": 220},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 100},
		{"label": _("Held By"), "fieldname": "held_by", "fieldtype": "Link", "options": "User", "width": 200},
		{"label": _("Created By"), "fieldname": "created_by", "fieldtype": "Link", "options": "User", "width": 200},
		{"label": _("Origin"), "fieldname": "origin", "fieldtype": "Data", "width": 130},
		{"label": _("Due"), "fieldname": "due", "fieldtype": "Datetime", "width": 160},
		{"label": _("Done On"), "fieldname": "done_on", "fieldtype": "Date", "width": 110},
	]
	return columns, rows


def _origin(task):
	if task.custom_workflow_token:
		return _("Workflow")
	if task.custom_automated:
		return _("Automation")
	if task.custom_lsq_task_id:
		return _("LeadSquared")
	return _("By hand")
