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
	_card("total_leads", "Leads", "In selected period", "number", "CRM Lead", base_filters=_UNCONVERTED, date_field="creation", honours_date_range=1),
	_card("total_tasks", "Tasks", "In selected period", "number", "CRM Task", date_field="creation", honours_date_range=1),
	# Bound by DUE DATE, not creation: dating it by creation answers a different question ("opened this
	# month"), but leaving it unbound asks an unbounded one on a screen that is a date range by nature —
	# and unbounded means `status IN (3)` alone, which matches 16% of the table, so MariaDB scans all
	# 646,026 rows rather than use an index. Due date is the column a rep acts on and the one indexed
	# beside status. Only 3 of 108,103 open tasks carry no due date, so nothing meaningful is dropped.
	_card("pending_tasks", "Open Tasks", "Due in selected period", "number", "CRM Task", base_filters={"status": _OPEN}, date_field="due_date", honours_date_range=1),
	# `between` and not `<`: frappe compares as ifnull(due_date, ''), so `<` counts every task that has NO due date as overdue.
	# NOT bound to the dashboard window, unlike its neighbours: the window's default runs to the END of the
	# current month, and a task due later this month is not overdue — letting the range set the upper bound
	# would count it. The cap stays `__NOW__` and the FLOOR does the narrowing instead, which is what makes
	# `ix_status_due_date` usable: 646,026 rows scanned becomes 17,131 sought.
	_card("overdue_tasks", "Overdue Tasks", f"Last {executor.OVERDUE_FLOOR_DAYS} days", "number", "CRM Task", base_filters={"status": _OPEN, "due_date": ["between", ["__OVERDUE_FLOOR__", "__NOW__"]]}),
	# CRM Task carries no completion date, so `modified` is the closest honest stamp for when it was closed.
	_card("completed_tasks", "Completed Tasks", "In selected period", "number", "CRM Task", base_filters={"status": "Done"}, date_field="modified", honours_date_range=1),
	# `timespan` is frappe's own relative-date operator (query.py:576 -> utils/data.py get_timespan_date_range).
	_card("tasks_due_today", "Tasks Due Today", "Today", "number", "CRM Task", base_filters={"status": _OPEN, "due_date": ["timespan", "today"]}),
	_card("leads_by_source", "Leads by Source", "In selected period", "donut", "CRM Lead", base_filters=_UNCONVERTED, group_by_field="source", date_field="creation", honours_date_range=1),
	_card("leads_by_vertical", "Leads by Product Line", "In selected period", "donut", "CRM Lead", base_filters=_UNCONVERTED, group_by_field="custom_vertical", date_field="creation", honours_date_range=1),
	_card("leads_by_substage", "Leads by Stage", "In selected period", "donut", "CRM Lead", base_filters=_UNCONVERTED, group_by_field="custom_substage", date_field="creation", honours_date_range=1),
	_card("leads_by_owner", "Leads by Owner", "In selected period", "donut", "CRM Lead", base_filters=_UNCONVERTED, group_by_field="lead_owner", date_field="creation", honours_date_range=1),
	# The two task bars are the same records cut two ways, so each names its own cut and neither says "status".
	_card("tasks_by_status", "Tasks by Status", "In selected period", "bar", "CRM Task", group_by_field="status", date_field="creation", honours_date_range=1),
	# `due_state` and `sla_state` are DERIVED fields, not columns — the executor resolves them through
	# `list_engine.derived`, which is why a group_by can name one and no schema change is needed.
	_card("tasks_by_due_state", "Tasks by Due State", "In selected period", "bar", "CRM Task", group_by_field="due_state", date_field="creation", honours_date_range=1, drill_enabled=1),
	_card("leads_by_sla_state", "Leads by SLA State", "In selected period", "bar", "CRM Lead", base_filters=_UNCONVERTED, group_by_field="sla_state", date_field="creation", honours_date_range=1, drill_enabled=1),
	# A miss is a lead whose response was due and never came: `response_by` in the past AND no
	# `first_responded_on`. Bounded by the window on `creation`, so the open-ended `response_by` floor
	# costs nothing — the range has already narrowed the rows.
	_card("leads_sla_missed_by_owner", "SLA Misses by Owner", "In selected period", "bar", "CRM Lead", base_filters={**_UNCONVERTED, "response_by": ["between", ["1900-01-01", "__NOW__"]], "first_responded_on": ["is", "not set"]}, group_by_field="lead_owner", date_field="creation", honours_date_range=1, drill_enabled=1),
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
	("tasks_by_due_state", 6, 14, 6, 6),
	("leads_by_sla_state", 0, 20, 6, 6),
	("leads_sla_missed_by_owner", 6, 20, 6, 6),
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
		existing = frappe.db.get_value(declaration.LAYOUT, {"role": row["role"]}, "name")
		if not existing:
			if frappe.db.exists("Role", row["role"]):
				frappe.get_doc({"doctype": declaration.LAYOUT, **row}).insert(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
			continue
		# Re-asserted exactly as a chart's STRUCTURAL fields are: which filters the page offers is this
		# app's behaviour, not the operator's wording, so a layout that predates a filter gets it.
		declared = {field: row[field] for field in declaration.LAYOUT_STRUCTURAL}
		stored = frappe.db.get_value(declaration.LAYOUT, existing, declaration.LAYOUT_STRUCTURAL, as_dict=True)
		if any(cstr(stored[field]) != cstr(declared[field]) for field in declaration.LAYOUT_STRUCTURAL):
			frappe.db.set_value(declaration.LAYOUT, existing, declared)  # authz-ok: tier-c — after_migrate, structural fields this app owns
	frappe.db.commit()


@frappe.whitelist()
def restate_copy():
	"""Push the DECLARED wording onto charts that already exist. A command, never part of the seed.

	`PRESENTATION` (label, subtitle) is seeded once and deliberately never re-imposed, so an operator who
	rewords a card keeps their words through every deploy. That rule is right, and it is also why editing
	the wording in this file changes nothing on a site that already has the rows — there is no fresh site
	any more. This is the explicit door: run it when the declared copy is meant to win.

	    bench --site <site> execute tatva_connect.dashboard.seed.restate_copy

	Touches only the charts this app declares. The three charts an operator built in the UI
	(leads_by_sla_state, tasks_by_due_state, leads_sla_missed_by_owner) are theirs and are not named here,
	so they are left exactly as written."""
	changed = []
	for row in _CHARTS:
		name = row["chart_name"]
		if not frappe.db.exists(declaration.CHART, name):
			continue
		declared = {field: row[field] for field in declaration.PRESENTATION}
		stored = frappe.db.get_value(declaration.CHART, name, declaration.PRESENTATION, as_dict=True)
		if any(cstr(stored[field]) != cstr(declared[field]) for field in declaration.PRESENTATION):
			frappe.db.set_value(declaration.CHART, name, declared)
			changed.append(name)
	frappe.db.commit()
	declaration.retire_cache()
	print(frappe.as_json({"restated": changed, "unchanged": len(_CHARTS) - len(changed)}))
	return changed
