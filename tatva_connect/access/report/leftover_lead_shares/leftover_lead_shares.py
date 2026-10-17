# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Lead shares to someone who neither owns the lead nor holds a live assignment on it, on leads the viewer can read."""
import frappe
from frappe import _
from frappe.query_builder import Order
from frappe.query_builder.functions import IfNull

LEAD = "CRM Lead"
ROWS = 1000


def execute(filters=None):
	frappe.has_permission(LEAD, "read", throw=True)
	share, lead, todo = frappe.qb.DocType("DocShare"), frappe.qb.DocType(LEAD), frappe.qb.DocType("ToDo")
	readable = frappe.qb.get_query(LEAD, fields=["name"], ignore_permissions=False)
	rows = (
		frappe.qb.from_(share)
		.join(lead).on(lead.name == share.share_name)
		.left_join(todo).on((todo.reference_type == LEAD) & (todo.reference_name == share.share_name)
			& (todo.allocated_to == share.user) & (todo.status != "Cancelled"))
		.select(share.share_name.as_("lead"), share.user.as_("can_open"), lead.lead_owner, lead.custom_vertical,
			lead.custom_group, lead.custom_current_program, share.creation.as_("shared_on"))
		.where((share.share_doctype == LEAD) & (share.user != IfNull(lead.lead_owner, ""))
			& todo.name.isnull() & share.share_name.isin(readable))
		.orderby(share.creation, order=Order.desc)
		.limit(ROWS)
	).run(as_dict=True)
	columns = [
		{"label": _("Lead"), "fieldname": "lead", "fieldtype": "Link", "options": LEAD, "width": 130},
		{"label": _("Can Open"), "fieldname": "can_open", "fieldtype": "Link", "options": "User", "width": 220},
		{"label": _("Lead Owner"), "fieldname": "lead_owner", "fieldtype": "Link", "options": "User", "width": 220},
		{"label": _("Vertical"), "fieldname": "custom_vertical", "fieldtype": "Link", "options": "CRM Vertical", "width": 110},
		{"label": _("Group"), "fieldname": "custom_group", "fieldtype": "Link", "options": "CRM Group", "width": 110},
		{"label": _("Program"), "fieldname": "custom_current_program", "fieldtype": "Link", "options": "CRM Program", "width": 130},
		{"label": _("Shared On"), "fieldname": "shared_on", "fieldtype": "Datetime", "width": 160},
	]
	return columns, rows
