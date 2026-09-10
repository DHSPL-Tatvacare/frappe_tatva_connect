"""Data method for the 'Inbound Leads' Dashboard Chart Source.

Thin: asks `observability.inbound_leads` which door each lead came through, returns {labels, datasets}.

There is no by-source view here on purpose. Grouping leads by their source is a native Group By chart
and building a second one was the defect: a custom source for something frappe already does is a
parallel implementation that can only drift. This exists for the one split frappe cannot express — the
doors are derived from the ingest configuration, not stored on the lead.
"""
import frappe

from tatva_connect.observability import inbound_leads


@frappe.whitelist()
def get(chart_name=None, chart=None, no_cache=None, filters=None, from_date=None, to_date=None,
	time_interval=None, timespan=None, heatmap_year=None, **kwargs):
	frappe.has_permission("CRM Lead", "read", throw=True)  # the same gate the figures themselves ask
	parsed = frappe.parse_json(filters) if filters else None
	parsed = parsed if isinstance(parsed, dict) else {}
	days = frappe.utils.cint(parsed.get("days")) or 30

	mapping = inbound_leads.by_path(days)
	return {
		"labels": list(mapping.keys()),
		"datasets": [{"name": frappe._("Leads"), "values": list(mapping.values())}],
	}
