# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The baseline dashboard catalogue: ten cards, and one layout that shows them.

The cards our code ships are asserted on every migrate; how an operator has LABELLED and ARRANGED them is
theirs and is never re-imposed. So a title reworded in Desk survives the next deploy, and a card silently
re-pointed at the wrong column does not.

ONE layout is seeded — System Manager. Every other role is deliberately left with none, so the
"no dashboard configured" answer is exercised on a real role from day one instead of being discovered
after a deploy. Giving the other roles a dashboard is a configuration step, not a code change: their
cards already exist here and a layout row is all that is missing.

Which cards a layout holds is the whole of what a role is shown, which is why the answer to "can a role
have a dashboard with no deal cards?" is yes by construction — the baseline declares none at all.

Plan: docs/plans/2026-07-31-dashboard-role-layouts-phase-1.md
"""

import json

import frappe
from frappe.utils import cstr

CHART_DOCTYPE = "CRM Dashboard Chart"
LAYOUT_DOCTYPE = "CRM Dashboard Layout"

# The three activity statuses that mean "still to do", named once and meant the same way in three cards.
_OPEN = ["in", ["Backlog", "Todo", "In Progress"]]


def _card(chart_name, label, subtitle, chart_type, source_doctype, **declared):
	"""One chart row, with the fields a card does not use left explicitly empty rather than absent."""
	row = {
		"chart_name": chart_name,
		"label": label,
		"subtitle": subtitle,
		"chart_type": chart_type,
		"source_doctype": source_doctype,
		"aggregate": "COUNT",
		"aggregate_field": "",
		"group_by_field": "",
		"label_field": "",
		"date_field": "",
		"honours_date_range": 0,
		"base_filters": {},
		"row_limit": 10,
		"drill_enabled": 1,
	}
	row.update(declared)
	row["base_filters"] = json.dumps(row["base_filters"])
	return row


_CHARTS = [
	_card("total_leads", "Leads", "Created in the selected range", "number", "CRM Lead", date_field="creation", honours_date_range=1),
	_card("total_tasks", "Activities", "Created in the selected range", "number", "CRM Task", date_field="creation", honours_date_range=1),
	# A snapshot, not a range: "still open" is a fact about now, and dating it by creation answers something else.
	_card("pending_tasks", "Pending Activities", "Open right now", "number", "CRM Task", base_filters={"status": _OPEN}),
	# `between` and not `<`: frappe compares as ifnull(due_date, ''), so `<` counts every activity that has NO due date as overdue.
	_card("overdue_tasks", "Overdue Activities", "Open and past their due date", "number", "CRM Task", base_filters={"status": _OPEN, "due_date": ["between", ["1900-01-01", "__now__"]]}),
	# CRM Task carries no completion date, so `modified` is the closest honest stamp for when it was closed.
	_card("completed_tasks", "Completed Activities", "Closed in the selected range", "number", "CRM Task", base_filters={"status": "Done"}, date_field="modified", honours_date_range=1),
	_card("tasks_due_today", "Due Today", "Open and due before midnight", "number", "CRM Task", base_filters={"status": _OPEN, "due_date": ["between", ["__today__", "__today__"]]}),
	_card("leads_by_source", "Leads by Source", "Where they came from", "donut", "CRM Lead", group_by_field="source", date_field="creation", honours_date_range=1),
	_card("leads_by_vertical", "Leads by Product Line", "Created in the selected range", "donut", "CRM Lead", group_by_field="custom_vertical", date_field="creation", honours_date_range=1),
	# Grouped on the owner column and LABELLED with the person's name: the name is display, the column filters.
	_card("leads_by_owner", "Leads by Owner", "Created in the selected range", "donut", "CRM Lead", group_by_field="lead_owner", label_field="lead_owner.full_name", date_field="creation", honours_date_range=1),
	_card("tasks_by_status", "Activities by Status", "Created in the selected range", "bar", "CRM Task", group_by_field="status", date_field="creation", honours_date_range=1),
]

# What a card MEANS: asserted every migrate. Label, subtitle and Enabled are the operator's, set once at insert.
_STRUCTURAL = ("chart_type", "source_doctype", "aggregate", "aggregate_field", "group_by_field",
               "label_field", "date_field", "honours_date_range", "base_filters", "row_limit",
               "drill_enabled")

_PLACED = (
	("total_leads", 0, 0, 2, 2),
	("total_tasks", 2, 0, 2, 2),
	("pending_tasks", 4, 0, 2, 2),
	("overdue_tasks", 6, 0, 2, 2),
	("tasks_due_today", 8, 0, 2, 2),
	("completed_tasks", 10, 0, 2, 2),
	("leads_by_source", 0, 2, 4, 4),
	("leads_by_vertical", 4, 2, 4, 4),
	("leads_by_owner", 8, 2, 4, 4),
	("tasks_by_status", 0, 6, 12, 4),
)

# Exactly one layout ships. A role with no row here has no dashboard, which is the answer, not a gap.
_LAYOUTS = [
	{
		"role": "System Manager",
		"title": "Dashboard",
		"enabled": 1,
		"priority": 100,
		"layout": json.dumps([{"chart": name, "x": x, "y": y, "w": w, "h": h} for name, x, y, w, h in _PLACED]),
		"exposed_filters": json.dumps(["date_range", "vertical", "program", "user"]),
	}
]


def ensure_rows():
	"""Idempotent: what our code depends on is asserted, and an operator's own arrangement is left alone."""
	# skip-until-ready: an early caller can run before these doctypes sync; the after_migrate pass seeds then.
	if not (frappe.db.table_exists(CHART_DOCTYPE) and frappe.db.table_exists(LAYOUT_DOCTYPE)):
		return
	for row in _CHARTS:
		if not frappe.db.exists(CHART_DOCTYPE, row["chart_name"]):
			frappe.get_doc({"doctype": CHART_DOCTYPE, "enabled": 1, **row}).insert(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
			continue
		declared = {field: row[field] for field in _STRUCTURAL}
		stored = frappe.db.get_value(CHART_DOCTYPE, row["chart_name"], _STRUCTURAL, as_dict=True)
		if any(cstr(stored[field]) != cstr(declared[field]) for field in _STRUCTURAL):
			frappe.db.set_value(CHART_DOCTYPE, row["chart_name"], declared)  # authz-ok: tier-c — after_migrate, structural fields this app owns
	for row in _LAYOUTS:
		# A layout is placement, and placement is the operator's: seeded once and never re-imposed.
		if not frappe.db.exists(LAYOUT_DOCTYPE, row["role"]) and frappe.db.exists("Role", row["role"]):
			frappe.get_doc({"doctype": LAYOUT_DOCTYPE, **row}).insert(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
	frappe.db.commit()
