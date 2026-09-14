"""Data method for the 'Automation Health by Grain' Dashboard Chart Source.

WHY THIS EXISTS AT ALL. `hooks.py` has stated since Workspace-P2 that health-by-grain "is now a native
Dashboard Chart (chart_type=Custom, source 'Automation Health by Grain')". The chart shipped; the source
never did, so it named a source no folder in this app provides and drew nothing. This is that source.

WHICH GRAIN. Not the workflow's `trigger_vertical/group/program` — those are Data fields, free text an
author typed to scope a trigger, and `grain.columns` rightly does not see them as grain columns. The
grain that matters is the one the SUBJECT carries, so the journey is joined to its lead and the axes
come from `grain.columns("CRM Lead")`, read off the schema and never typed here.

The join is a LEFT one and an unresolvable subject buckets as its own grain rather than vanishing, so
the bars still sum to every journey in the window.

No separate brain module: `jobs_health` and `inbound_leads` exist because cards, charts and the Control
Tower all ask them the same question. Nothing but this chart asks this one.
"""
import frappe
from frappe.desk.reportview import get_match_cond
from frappe.utils import add_to_date, cint, now_datetime

from tatva_connect.taxonomy import grain

JOURNEY_DT = "CRM Workflow Journey"


@frappe.whitelist()
def get(chart_name=None, chart=None, no_cache=None, filters=None, from_date=None, to_date=None,
	time_interval=None, timespan=None, heatmap_year=None, **kwargs):
	frappe.has_permission(JOURNEY_DT, "read", throw=True)
	parsed = frappe.parse_json(filters) if filters else None
	parsed = parsed if isinstance(parsed, dict) else {}
	days = cint(parsed.get("days")) or 7

	# Identifiers come from `get_meta` via grain.columns, never from a request; the window is bound.
	axes = ", ".join(
		f"COALESCE(NULLIF(l.`{column}`, ''), '—')" for column in grain.columns("CRM Lead") if column
	)
	rows = frappe.db.sql(  # sqli-ok: `axes` is column NAMES from grain.columns (get_meta) and the match condition is frappe's own; the window is bound
		f"""SELECT CONCAT_WS('::', {axes}) AS grain,
		           COUNT(*) AS runs,
		           SUM(`tab{JOURNEY_DT}`.status = 'Failed') AS failed
		    FROM `tab{JOURNEY_DT}`
		    LEFT JOIN `tabCRM Lead` l ON l.name = `tab{JOURNEY_DT}`.subject_name AND `tab{JOURNEY_DT}`.subject_doctype = 'CRM Lead'
		    WHERE `tab{JOURNEY_DT}`.creation >= %(since)s {get_match_cond(JOURNEY_DT)}
		    GROUP BY grain
		    ORDER BY runs DESC""",
		{"since": add_to_date(now_datetime(), days=-days)},
		as_dict=True,
	)
	return {
		"labels": [r.grain for r in rows],
		"datasets": [
			{"name": frappe._("Journeys"), "values": [cint(r.runs) for r in rows]},
			{"name": frappe._("Failed"), "values": [cint(r.failed) for r in rows]},
		],
	}
