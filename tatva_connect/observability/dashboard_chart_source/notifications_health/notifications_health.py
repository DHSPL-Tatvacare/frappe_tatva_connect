"""Data method for the 'Notifications Health' Dashboard Chart Source: picks a view and returns `notifications_health.daily`."""
import frappe

from tatva_connect.observability import notifications_health


@frappe.whitelist()
def get(chart_name=None, chart=None, no_cache=None, filters=None, from_date=None, to_date=None,
	time_interval=None, timespan=None, heatmap_year=None, **kwargs):
	frappe.only_for("System Manager")
	parsed = frappe.parse_json(filters) if filters else None
	view = parsed.get("view") if isinstance(parsed, dict) else None  # the config UI can send a list form
	return notifications_health.daily(view or "type")
