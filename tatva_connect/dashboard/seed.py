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

from tatva_connect.dashboard import declaration, executor
from tatva_connect.taxonomy import grain

# The three task statuses that mean "still to do", named once and meant the same way in three cards.
_OPEN = ["in", ["Backlog", "Todo", "In Progress"]]

# What the Leads list itself shows: a card counting converted leads would open a list that excludes them, so the card says it too.
_UNCONVERTED = {"converted": 0}


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
		"split_by": "",
		"time_bucket": declaration.NO_BUCKET,
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
	_card("total_leads", "Leads", "Created in range", "number", "CRM Lead", base_filters=_UNCONVERTED, date_field="creation", honours_date_range=1),
	_card("total_tasks", "Tasks", "Created in range", "number", "CRM Task", date_field="creation", honours_date_range=1),
	# A snapshot, not a range: "still open" is a fact about now, and dating it by creation answers something else.
	_card("pending_tasks", "Open Tasks", "Open right now", "number", "CRM Task", base_filters={"status": _OPEN}),
	# `between` and not `<`: frappe compares as ifnull(due_date, ''), so `<` counts every task that has NO due date as overdue.
	_card("overdue_tasks", "Overdue Tasks", "Past due date", "number", "CRM Task", base_filters={"status": _OPEN, "due_date": ["between", ["1900-01-01", "__NOW__"]]}),
	# CRM Task carries no completion date, so `modified` is the closest honest stamp for when it was closed.
	_card("completed_tasks", "Completed Tasks", "Closed in range", "number", "CRM Task", base_filters={"status": "Done"}, date_field="modified", honours_date_range=1),
	# `timespan` is frappe's own relative-date operator (query.py:576 -> utils/data.py get_timespan_date_range).
	_card("tasks_due_today", "Due Today", "Due before midnight", "number", "CRM Task", base_filters={"status": _OPEN, "due_date": ["timespan", "today"]}),
	_card("leads_by_source", "Leads by Source", "Acquisition channel", "donut", "CRM Lead", base_filters=_UNCONVERTED, group_by_field="source", date_field="creation", honours_date_range=1),
	_card("leads_by_vertical", "Leads by Product Line", "Product line split", "donut", "CRM Lead", base_filters=_UNCONVERTED, group_by_field="custom_vertical", date_field="creation", honours_date_range=1),
	_card("leads_by_substage", "Leads by Stage", "Current stage", "donut", "CRM Lead", base_filters=_UNCONVERTED, group_by_field="custom_substage", date_field="creation", honours_date_range=1),
	_card("leads_by_owner", "Leads by Owner", "Ownership split", "donut", "CRM Lead", base_filters=_UNCONVERTED, group_by_field="lead_owner", date_field="creation", honours_date_range=1),
	# The two task bars are the same records cut two ways, so each names its own cut and neither says "status".
	_card("tasks_by_status", "Task Progress", "Workflow state", "bar", "CRM Task", group_by_field="status", date_field="creation", honours_date_range=1),
	# A snapshot, like the two open-task cards: who is carrying what RIGHT NOW, not who was given work in a window.
	_card("tasks_by_owner_and_status", "Workload by Owner", "Open tasks now", "heatmap", "CRM Task", group_by_field="assigned_to", split_by="status", base_filters={"status": _OPEN}),
]


# h is in 60px grid rows. A chart must clear frappe-ui's `min-h-[300px]`, so h=6 (360px) and never h=4 (240px), which clips the ring. A number card has no minimum.
_PLACED = (
	("total_leads", 0, 0, 2, 2),
	("total_tasks", 2, 0, 2, 2),
	("pending_tasks", 4, 0, 2, 2),
	("overdue_tasks", 6, 0, 2, 2),
	("tasks_due_today", 8, 0, 2, 2),
	("completed_tasks", 10, 0, 2, 2),
	("leads_by_source", 0, 2, 6, 6),
	("leads_by_vertical", 6, 2, 6, 6),
	("leads_by_owner", 0, 8, 6, 6),
	("leads_by_substage", 6, 8, 6, 6),
	("tasks_by_status", 0, 14, 6, 6),
	("tasks_by_owner_and_status", 0, 20, 12, 6),
)


def filters_shipped():
	"""The controls a dashboard may offer: a period, the person, and one per grain column — and the grain
	columns are READ FROM THE SCHEMA, so this cannot drift from what frappe actually gates on."""
	return (executor.DATE_RANGE, "user", *(c for c in grain.columns("CRM Lead") if c))


def _placed():
	"""The shipped arrangement, as rows. One list, however many layouts read it — a second copy would drift."""
	return [{"chart": name, "x": x, "y": y, "w": w, "h": h} for name, x, y, w, h in _PLACED]


# A role with no row here has no dashboard, which is the answer, not a gap. Two ship, and they carry the
# SAME cards on purpose: what separates a manager from an administrator is not the questions asked, it is
# the rows the answers are drawn from, and that is already decided per viewer (access.visibility) rather
# than per layout. `Sales Manager` sits above `Sales User` so the 56 people who hold both stop landing on
# the rep's dashboard, and below `System Manager` so an administrator's own view is unchanged.
_LAYOUTS = [
	{
		"role": "System Manager",
		# `CRM Dashboard` autonames on the title; nothing reads the name, so an operator may reword it.
		"title": "System Manager Dashboard",
		"enabled": 1,
		"priority": 100,
		"charts": _placed(),
		"exposed_filters": json.dumps(list(filters_shipped())),
	},
	{
		"role": "Sales Manager",
		"title": "Sales Manager Dashboard",
		"enabled": 1,
		"priority": 75,
		"charts": _placed(),
		"exposed_filters": json.dumps(list(filters_shipped())),
	},
]


def ensure_rows():
	"""Idempotent: what our code depends on is asserted, and an operator's own arrangement is left alone."""
	# skip-until-ready: an early caller can run before these doctypes sync; the after_migrate pass seeds then.
	if not (frappe.db.table_exists(declaration.CHART) and frappe.db.table_exists(declaration.LAYOUT)):
		return
	# skip-until-ready: `role` is a custom field, so it lands at sync_fixtures — before after_migrate, not before a patch.
	if not frappe.get_meta(declaration.LAYOUT).get_field("role"):
		return
	for row in _CHARTS:
		if not frappe.db.exists(declaration.CHART, row["chart_name"]):
			frappe.get_doc({"doctype": declaration.CHART, "enabled": 1, **row}).insert(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
			continue
		declared = {field: row[field] for field in declaration.STRUCTURAL}
		stored = frappe.db.get_value(declaration.CHART, row["chart_name"], declaration.STRUCTURAL, as_dict=True)
		if any(cstr(stored[field]) != cstr(declared[field]) for field in declaration.STRUCTURAL):
			frappe.db.set_value(declaration.CHART, row["chart_name"], declared)  # authz-ok: tier-c — after_migrate, structural fields this app owns
	for row in _LAYOUTS:
		# Matched on the ROLE, never the name: the name is a title an operator may reword, and that is not a delete.
		if not frappe.db.exists(declaration.LAYOUT, {"role": row["role"]}) and frappe.db.exists("Role", row["role"]):
			frappe.get_doc({"doctype": declaration.LAYOUT, **row}).insert(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
	frappe.db.commit()
