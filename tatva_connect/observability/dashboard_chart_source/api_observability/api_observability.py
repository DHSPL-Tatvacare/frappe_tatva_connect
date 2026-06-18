"""Data method for the 'API Observability' Dashboard Chart Source.

Thin: parses the source filters (metric/granularity/dimensions), resolves the window from
the chart's timespan, and delegates the actual computation to observability.analytics
(shared with the number cards). Returns the Frappe chart shape {labels, datasets}.
"""
import frappe
from frappe.utils.dashboard import cache_source

from tatva_connect.observability import analytics


def _as_dict(f):
	"""Custom-source filters arrive as a dict; tolerate the [dt, field, op, value] list form."""
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
@cache_source
def get(
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

	metric = filters.get("metric") or "Requests"
	granularity = filters.get("granularity") or "day"
	frm, to = analytics._window(timespan, from_date, to_date)

	labels, values = analytics.series(metric, granularity, frm, to, filters)
	return {"labels": labels, "datasets": [{"name": metric, "values": values}]}
