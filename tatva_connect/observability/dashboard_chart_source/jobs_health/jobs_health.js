// Dashboard Chart Source: Jobs Health
// Registered for chart_type=Custom charts. read_config() serves this file; the Desk evals it
// to register the source's data method + the filter fields shown in the chart config UI.
frappe.provide("frappe.dashboards.chart_sources");

frappe.dashboards.chart_sources["Jobs Health"] = {
	method: "tatva_connect.observability.dashboard_chart_source.jobs_health.jobs_health.get",
	filters: [
		{
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: ["lanes", "status", "scheduled"].join("\n"),
			default: "lanes",
			reqd: 1,
		},
	],
};
