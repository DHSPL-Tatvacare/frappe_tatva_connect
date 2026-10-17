// Dashboard Chart Source: Traffic by Channel, registered for chart_type=Custom charts.
frappe.provide("frappe.dashboards.chart_sources");

frappe.dashboards.chart_sources["Traffic by Channel"] = {
	method: "tatva_connect.communications.dashboard_chart_source.traffic_by_channel.traffic_by_channel.get",
	filters: [],
};
