"""Data method for the 'Automation Health by Grain' Dashboard Chart Source.

Thin: gates on CRM Automation Run Log read, then delegates the aggregation to
tatva_connect.automation.report.grain_health — the SAME per-grain outcome counts the
Run Log report surface reads (one brain, A.8, no duplicate query/grouping). Returns the
Frappe chart shape {labels, datasets} for a stacked Success/Partial/Failed bar; the chart
RECORD (not this module) owns stacking + color, per the native theme.
"""
import frappe
from frappe.utils.dashboard import cache_source

from tatva_connect.automation.report import grain_health


def _as_dict(f):
	"""Custom-source filters arrive as a dict; tolerate the [dt, field, op, value] list form
	(mirrors observability.dashboard_chart_source.api_observability._as_dict)."""
	if isinstance(f, dict):
		return f
	out = {}
	for item in f or []:
		if isinstance(item, (list, tuple)):
			if len(item) >= 4:
				out[item[1]] = item[3]
			elif len(item) == 2:
				out[item[0]] = item[1]
	return out


@frappe.whitelist()
def get(chart_name=None, chart=None, no_cache=None, filters=None, from_date=None, to_date=None,
	time_interval=None, timespan=None, heatmap_year=None, **kwargs):
	# Gate BEFORE the cache. cache_source serves cached results without running the body, and its key
	# is per-chart (not per-user) — an in-body check would be bypassed on a cache hit. So the public
	# entry checks permission first, then delegates to the cached worker (same posture as API
	# Observability's source). grain_health() re-checks the same permission internally too (A.8 reuse,
	# not a bypass) — belt-and-braces, never widened.
	frappe.has_permission("CRM Automation Run Log", "read", throw=True)
	return _get(chart_name=chart_name, chart=chart, no_cache=no_cache, filters=filters,
		from_date=from_date, to_date=to_date, time_interval=time_interval, timespan=timespan,
		heatmap_year=heatmap_year, **kwargs)


@cache_source
def _get(
	chart_name=None,
	chart=None,
	no_cache=None,
	filters=None,
	from_date=None,
	to_date=None,
	time_interval=None,
	timespan=None,
	heatmap_year=None,
	**kwargs,
):
	filters = frappe.parse_json(filters) if isinstance(filters, str) else filters
	if not filters and chart_name:
		filters = frappe.parse_json(frappe.db.get_value("Dashboard Chart", chart_name, "filters_json") or "{}")
	filters = _as_dict(filters)

	rows = grain_health(days=filters.get("days"))
	return {
		"labels": [row["grain"] for row in rows],
		"datasets": [
			{"name": "Success", "values": [row["success"] for row in rows]},
			{"name": "Partial", "values": [row["partial"] for row in rows]},
			{"name": "Failed", "values": [row["failed"] for row in rows]},
		],
		"type": "bar",
	}
