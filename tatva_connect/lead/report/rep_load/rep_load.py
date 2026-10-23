# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Per rep: leads given to them in the window, and what they hold now, so an overloaded or starved rep shows at a glance; with a pool, one day of it against the pool's weights."""
import frappe
from frappe import _
from frappe.utils import add_days, getdate, now_datetime, today

from tatva_connect.access.team import team_of
from tatva_connect.lead.checkin import statuses as checkin_statuses
from tatva_connect.tasks.tasks import open_statuses

LEAD = "CRM Lead"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.only_for(("System Manager", "Sales Manager"))
	team = team_of(frappe.session.user)
	if filters.pool:
		return _pool_day(filters.pool, getdate(filters.date or today()), team, set(frappe.parse_json(filters.reps or "[]")))
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


def _pool_day(pool, day, team, picked=None):
	"""One pool's day for a manager: is volume getting out, is it split by weight, who is not working their leads, who is working now. Every read is by index."""
	from tatva_connect.lead import routing
	from tatva_connect.lead.assignment_rule import WEIGHTED
	from tatva_connect.workflow_engine import interpreter

	rule = frappe.get_doc("Assignment Rule", pool)
	rule.check_permission("read")
	weighted = rule.rule in WEIGHTED
	weights = {m.user: (m.get("weight") or 1) if weighted else 1 for m in rule._members()}  # the one grid reader for every strategy
	reps = [rep for rep in weights if (team is None or rep in team) and (not picked or rep in picked)]
	waiting = frappe.db.count(interpreter.JOURNEY_DT, routing._waiting(pool))
	if not reps:
		return _pool_columns(), [], None, None, _cards(waiting, 0, waiting, 0)
	# ToDo (allocated_to, assignment_rule, creation): today's picks from this pool, first assignments and hand-offs alike.
	window = [["allocated_to", "in", reps], ["assignment_rule", "=", pool], ["creation", ">=", day], ["creation", "<", add_days(day, 1)]]
	todos = frappe.get_all("ToDo", filters=window, fields=["allocated_to", "reference_name", "status"])
	# Only leads the viewer may read count, checked on the day's own leads by primary key, never a scan of every lead.
	names = list({t.reference_name for t in todos})
	readable = set(frappe.get_list(LEAD, filters={"name": ["in", names]}, pluck="name", limit_page_length=0)) if names else set()
	todos = [t for t in todos if t.reference_name in readable]
	got, moved = dict.fromkeys(reps, 0), dict.fromkeys(reps, 0)
	for t in todos:
		got[t.allocated_to] += 1
		moved[t.allocated_to] += t.status == "Cancelled"  # a pool ToDo cancelled: moved by routing or by a manager, as `routing._reassigns` counts it
	untouched = routing.untouched_counts(pool, reps)  # the count the untouched limit reads
	status = checkin_statuses(reps)
	total_weight, total_got = sum(weights[rep] for rep in reps), len(todos)
	rows = []
	for rep in reps:
		target = 100 * weights[rep] / total_weight
		actual = 100 * got[rep] / total_got if total_got else 0
		rows.append({"rep": rep, "status": status.get(rep) or _("Never checked in"), "target_pct": round(target, 1), "got": got[rep],
		             "actual_pct": round(actual, 1), "vs_target": round(actual - target, 1) if total_got else None,
		             "untouched": untouched.get(rep, 0), "moved": moved[rep]})
	rows.sort(key=lambda r: -r["target_pct"])
	return _pool_columns(), rows, None, None, _cards(len(readable) + waiting, len(readable), waiting, sum(moved.values()))


def _cards(leads_in, assigned, waiting, moved):
	return [
		{"label": _("Leads in today"), "value": leads_in, "datatype": "Int", "indicator": "Blue"},
		{"label": _("Assigned"), "value": assigned, "datatype": "Int", "indicator": "Green"},
		{"label": _("Waiting for a rep"), "value": waiting, "datatype": "Int", "indicator": "Orange" if waiting else "Green"},
		{"label": _("Taken away"), "value": moved, "datatype": "Int", "indicator": "Red" if moved else "Green"},
	]


def _pool_columns():
	return [
		{"label": _("Rep"), "fieldname": "rep", "fieldtype": "Link", "options": "User", "width": 240},
		{"label": _("Status Now"), "fieldname": "status", "fieldtype": "Data", "width": 120},
		{"label": _("Target %"), "fieldname": "target_pct", "fieldtype": "Percent", "width": 95},
		{"label": _("Assigned Today"), "fieldname": "got", "fieldtype": "Int", "width": 100},
		{"label": _("Actual %"), "fieldname": "actual_pct", "fieldtype": "Percent", "width": 95},
		{"label": _("vs Target"), "fieldname": "vs_target", "fieldtype": "Float", "precision": 1, "width": 95},
		{"label": _("Untouched Now"), "fieldname": "untouched", "fieldtype": "Int", "width": 120},
		{"label": _("Taken Away"), "fieldname": "moved", "fieldtype": "Int", "width": 110},
	]


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
