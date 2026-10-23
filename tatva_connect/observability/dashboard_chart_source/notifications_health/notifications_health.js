// Dashboard Chart Source: Notifications Health
frappe.provide("frappe.dashboards.chart_sources");

frappe.dashboards.chart_sources["Notifications Health"] = {
	method: "tatva_connect.observability.dashboard_chart_source.notifications_health.notifications_health.get",
	filters: [
		{
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: ["type", "read_rate", "push", "app"].join("\n"),
			default: "type",
			reqd: 1,
		},
	],
};
