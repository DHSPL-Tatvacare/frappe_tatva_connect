# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Automation Queue — what is waiting, what is overdue, and which lead is where.

One row per parked execution of a rule (`CRM Automation Resume`), enriched with the lead it acts on and
the frozen `CRM Automation Rule Version` it is executing. The summary answers the operational questions
in order of urgency:

    Due now   — waiting executions whose Wait has elapsed. A non-zero, non-falling number means the
                sweep is not draining: check the scheduler and the two automation switches.
    Waiting   — parked and not yet due. The healthy backlog of a drip campaign.
    Failed    — a segment errored on resume; the rest of that lead's chain will never run.
    Cancelled — the rule was deleted under them.

Reads are permission-gated (S.1) and go through `frappe.get_all` with bound filters (S.2) — no raw SQL.
Lead names, grains and version numbers are resolved in ONE batched read each, never per row.
"""
import frappe
from frappe import _

RESUME_DT = "CRM Automation Resume"
_LEAD_FIELDS = ("name", "lead_name", "custom_vertical", "custom_group", "custom_current_program")


def execute(filters=None):
	frappe.has_permission(RESUME_DT, "read", throw=True)
	filters = frappe._dict(filters or {})

	rows = _queue_rows(filters)
	# The subject leads drive both the enrichment AND the row's own visibility: a queue row exposes a
	# patient's name and grain, so a caller who cannot see the lead must not see its execution either.
	# `get_list` (permission-checked, honours the CRM Lead grain scope) is the ONE gate — a grain-scoped
	# Sales Manager sees only their grain's queue, never another grain's patient names (S.1).
	leads = _visible_leads({r.subject_name for r in rows})
	rows = [r for r in rows if r.subject_name in leads]
	versions = _by_name(
		"CRM Automation Rule Version", {r.rule_version for r in rows if r.rule_version}, ("name", "version_no", "action_count")
	)

	now = frappe.utils.now_datetime()
	data = [_row(r, leads, versions, now) for r in rows]
	return _columns(), data, None, _chart(data), _summary(data)


def _queue_rows(filters):
	conditions = {}
	if filters.rule:
		conditions["rule"] = filters.rule
	if filters.status:
		conditions["status"] = filters.status
	if filters.from_date and filters.to_date:
		conditions["resume_at"] = ["between", [filters.from_date, filters.to_date]]
	return frappe.get_all(
		RESUME_DT,
		filters=conditions,
		fields=["name", "rule", "rule_version", "subject_name", "cursor", "parked_at", "resume_at", "status", "status_reason"],
		order_by="resume_at asc",
		limit=int(filters.limit or 500),
	)


def _visible_leads(names):
	"""The caller's VISIBLE subject leads, keyed by name. `get_list` applies the CRM Lead permission
	query, so a lead outside the caller's grain never comes back — and its queue row is dropped. The
	`has_permission` check first is what turns "no lead access at all" into an empty result instead of a
	raise (`get_list` throws for a caller with zero read on the doctype). ONE batched read, never per row."""
	if not names or not frappe.has_permission("CRM Lead", "read"):
		return {}
	rows = frappe.get_list(
		"CRM Lead", filters={"name": ["in", list(names)]}, fields=list(_LEAD_FIELDS), limit_page_length=0
	)
	return {r.name: r for r in rows}


def _by_name(doctype, names, fields):
	"""ONE batched read per lookup doctype — never a query per queue row. For non-lead lookups only
	(the version metadata); lead visibility is `_visible_leads`."""
	if not names:
		return {}
	return {r.name: r for r in frappe.get_all(doctype, filters={"name": ["in", list(names)]}, fields=list(fields))}


def _row(r, leads, versions, now):
	lead = leads.get(r.subject_name) or frappe._dict()
	version = versions.get(r.rule_version) or frappe._dict()
	overdue = r.status == "Pending" and r.resume_at and r.resume_at <= now
	return {
		"resume": r.name,
		"status": _("Due now") if overdue else r.status,
		"lead": r.subject_name,
		"lead_name": lead.lead_name,
		"grain": "::".join(x for x in (lead.custom_vertical, lead.custom_group, lead.custom_current_program) if x),
		"rule": r.rule,
		"rule_version": r.rule_version,
		"version_no": version.version_no,
		# The Wait it sleeps in is the action at `cursor - 1`; the next action to run is at `cursor`.
		"step": f"{r.cursor}/{version.action_count}" if version.action_count else str(r.cursor),
		"parked_at": r.parked_at,
		"resume_at": r.resume_at,
		"waiting_days": frappe.utils.date_diff(now, r.parked_at) if r.parked_at else None,
		"status_reason": r.status_reason,
	}


def _columns():
	return [
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 100},
		{"fieldname": "lead", "label": _("Lead"), "fieldtype": "Link", "options": "CRM Lead", "width": 150},
		{"fieldname": "lead_name", "label": _("Lead Name"), "fieldtype": "Data", "width": 170},
		{"fieldname": "grain", "label": _("Grain"), "fieldtype": "Data", "width": 200},
		{"fieldname": "rule", "label": _("Rule"), "fieldtype": "Link", "options": "CRM Automation Rule", "width": 240},
		{"fieldname": "version_no", "label": _("Ver"), "fieldtype": "Int", "width": 60},
		{"fieldname": "step", "label": _("Step"), "fieldtype": "Data", "width": 70},
		{"fieldname": "resume_at", "label": _("Resumes At"), "fieldtype": "Datetime", "width": 160},
		{"fieldname": "waiting_days", "label": _("Waiting (d)"), "fieldtype": "Int", "width": 100},
		{"fieldname": "parked_at", "label": _("Parked At"), "fieldtype": "Datetime", "width": 160},
		{"fieldname": "rule_version", "label": _("Version"), "fieldtype": "Link", "options": "CRM Automation Rule Version", "width": 120},
		{"fieldname": "status_reason", "label": _("Reason"), "fieldtype": "Data", "width": 300},
		{"fieldname": "resume", "label": _("Queue Row"), "fieldtype": "Link", "options": RESUME_DT, "width": 100},
	]


def _counts(data):
	tally = {}
	for row in data:
		tally[row["status"]] = tally.get(row["status"], 0) + 1
	return tally


def _summary(data):
	tally = _counts(data)
	due = tally.get(_("Due now"), 0)
	return [
		{"label": _("Due now"), "value": due, "datatype": "Int", "indicator": "Red" if due else "Green"},
		{"label": _("Waiting"), "value": tally.get("Pending", 0), "datatype": "Int", "indicator": "Blue"},
		{"label": _("Done"), "value": tally.get("Done", 0), "datatype": "Int", "indicator": "Green"},
		{"label": _("Failed"), "value": tally.get("Failed", 0), "datatype": "Int",
		 "indicator": "Red" if tally.get("Failed") else "Grey"},
		{"label": _("Cancelled"), "value": tally.get("Cancelled", 0), "datatype": "Int", "indicator": "Grey"},
	]


def _chart(data):
	tally = _counts(data)
	labels = [label for label in (_("Due now"), "Pending", "Done", "Failed", "Cancelled") if tally.get(label)]
	return {
		"data": {"labels": labels, "datasets": [{"name": _("Executions"), "values": [tally[label] for label in labels]}]},
		"type": "donut",
	}
