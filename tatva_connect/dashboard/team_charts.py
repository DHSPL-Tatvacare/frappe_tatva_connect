# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Team-productivity charts for the CRM SPA "Manager Dashboard" — task KPIs, Leads by Owner, Tasks by
Owner. Modelled on the LeadSquared "Sales Productivity" board.

Dispatched from the fork via thin `# TATVA` `get_*` shims (logic lives here). Two conventions:

- Task owner = `COALESCE(assigned_to, owner)` — `assigned_to` is only ~7% populated on migrated tasks,
  so we fall back to the doc owner. This is the SAME `assigned_to or owner` rule activity/api.py uses.
- Task KPIs and Tasks-by-Owner are a CURRENT-STATE snapshot (they ignore the dashboard date window) —
  "how much is pending/overdue right now". Leads-by-Owner is a volume chart and respects the window,
  like Leads by Product Line / Source.
"""
import frappe
from frappe import _
from frappe.query_builder import Case, DocType
from frappe.query_builder.functions import Coalesce, Count, Date, Sum

from tatva_connect.access import visibility

# Open = not yet closed. CRM Task status is one of Backlog / Todo / In Progress / Done / Canceled.
_OPEN = ["Backlog", "Todo", "In Progress"]
_TOP_N = 10


def _range(from_date, to_date):
	if not from_date or not to_date:
		from_date = frappe.utils.get_first_day(from_date or frappe.utils.nowdate())
		to_date = frappe.utils.get_last_day(to_date or frappe.utils.nowdate())
	return from_date, to_date


def _owner(Task):
	"""The effective task owner — the rep it's assigned to, else whoever created it."""
	return Coalesce(Task.assigned_to, Task.owner)


def _count(query):
	return query.run()[0][0] or 0


def _kpi(title, tooltip, value):
	return {"title": title, "tooltip": tooltip, "value": value or 0}


# ---- Task KPIs (current-state snapshot; scoped to the caller when a rep is viewing) ----


def total_tasks(user=None):
	Task = DocType("CRM Task")
	q = visibility.scope(frappe.qb.from_(Task).select(Count("*")), "CRM Task", Task)
	if user:
		q = q.where(_owner(Task) == user)
	return _kpi(_("Total Tasks"), _("All tasks"), _count(q))


def pending_tasks(user=None):
	Task = DocType("CRM Task")
	q = visibility.scope(frappe.qb.from_(Task).select(Count("*")).where(Task.status.isin(_OPEN)), "CRM Task", Task)
	if user:
		q = q.where(_owner(Task) == user)
	return _kpi(_("Pending Tasks"), _("Tasks not yet completed"), _count(q))


def overdue_tasks(user=None):
	Task = DocType("CRM Task")
	q = (
		frappe.qb.from_(Task)
		.select(Count("*"))
		.where(Task.status.isin(_OPEN))
		.where(Task.due_date.isnotnull())
		.where(Task.due_date < frappe.utils.now())
	)
	q = visibility.scope(q, "CRM Task", Task)
	if user:
		q = q.where(_owner(Task) == user)
	return _kpi(_("Overdue Tasks"), _("Pending tasks past their due date"), _count(q))


def tasks_due_today(user=None):
	Task = DocType("CRM Task")
	q = (
		frappe.qb.from_(Task)
		.select(Count("*"))
		.where(Task.status.isin(_OPEN))
		.where(Date(Task.due_date) == frappe.utils.nowdate())
	)
	q = visibility.scope(q, "CRM Task", Task)
	if user:
		q = q.where(_owner(Task) == user)
	return _kpi(_("Due Today"), _("Pending tasks due today"), _count(q))


def completed_tasks(user=None):
	Task = DocType("CRM Task")
	q = visibility.scope(frappe.qb.from_(Task).select(Count("*")).where(Task.status == "Done"), "CRM Task", Task)
	if user:
		q = q.where(_owner(Task) == user)
	return _kpi(_("Completed Tasks"), _("Tasks marked done"), _count(q))


# ---- Distribution charts ----


def leads_by_owner(from_date=None, to_date=None, user=None):
	"""Leads grouped by owner (volume — respects the date window). Label = the owner's full name."""
	from_date, to_date = _range(from_date, to_date)
	Lead = DocType("CRM Lead")
	User = DocType("User")
	query = (
		frappe.qb.from_(Lead)
		.left_join(User)
		.on(Lead.lead_owner == User.name)
		.select(Coalesce(User.full_name, Lead.lead_owner, _("Unassigned")).as_("owner"), Count("*").as_("count"))
		.where(Date(Lead.creation).between(from_date, to_date))
		.groupby(Lead.lead_owner)
		.orderby(Count("*"), order=frappe.qb.desc)
		.limit(_TOP_N)
	)
	query = visibility.scope(query, "CRM Lead", Lead)
	if user:
		query = query.where(Lead.lead_owner == user)
	return {
		"data": query.run(as_dict=True) or [],
		"title": _("Leads by Owner"),
		"subtitle": _("Top {0} owners").format(_TOP_N),
		"categoryColumn": "owner",
		"valueColumn": "count",
	}


def tasks_by_owner(user=None):
	"""Per-owner task workload as a stacked bar — Completed / Overdue / Pending (mutually exclusive, so
	the stack sums to the owner's non-cancelled total). Current-state snapshot, top owners by volume."""
	Task = DocType("CRM Task")
	User = DocType("User")
	owner = _owner(Task)
	now = frappe.utils.now()
	done = Case().when(Task.status == "Done", 1).else_(0)
	overdue = Case().when(
		(Task.status.isin(_OPEN)) & (Task.due_date.isnotnull()) & (Task.due_date < now), 1
	).else_(0)
	pending = Case().when(
		(Task.status.isin(_OPEN)) & ((Task.due_date.isnull()) | (Task.due_date >= now)), 1
	).else_(0)

	query = (
		frappe.qb.from_(Task)
		.left_join(User)
		.on(owner == User.name)
		.select(
			Coalesce(User.full_name, owner).as_("owner"),
			Sum(done).as_("Completed"),
			Sum(overdue).as_("Overdue"),
			Sum(pending).as_("Pending"),
		)
		.groupby(owner)
		.orderby(Count("*"), order=frappe.qb.desc)
		.limit(_TOP_N)
	)
	query = visibility.scope(query, "CRM Task", Task)
	if user:
		query = query.where(owner == user)
	return {
		"data": query.run(as_dict=True) or [],
		"title": _("Tasks by Owner"),
		"subtitle": _("Top {0} owners · current workload").format(_TOP_N),
		"xAxis": {"title": _("Owner"), "key": "owner", "type": "category"},
		"yAxis": {"title": _("Tasks")},
		"swapXY": True,
		"stacked": True,
		"series": [
			{"name": "Completed", "type": "bar"},
			{"name": "Overdue", "type": "bar"},
			{"name": "Pending", "type": "bar"},
		],
	}
