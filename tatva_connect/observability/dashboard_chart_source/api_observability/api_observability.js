// Dashboard Chart Source: API Observability
// Registered for chart_type=Custom charts. read_config() serves this file; the Desk evals it
// to register the source's data method + the filter fields shown in the chart config UI.
frappe.provide("frappe.dashboards.chart_sources");

frappe.dashboards.chart_sources["API Observability"] = {
	method: "tatva_connect.observability.dashboard_chart_source.api_observability.api_observability.get",
	filters: [
		{
			fieldname: "metric",
			label: __("Metric"),
			fieldtype: "Select",
			options: ["Requests", "Errors", "Error Rate", "Avg", "p50", "p95", "p99"].join("\n"),
			default: "Requests",
			reqd: 1,
		},
		{
			fieldname: "granularity",
			label: __("Granularity"),
			fieldtype: "Select",
			options: ["5min", "hour", "day"].join("\n"),
			default: "day",
			reqd: 1,
		},
		{
			fieldname: "channel",
			label: __("Channel"),
			fieldtype: "Select",
			options: ["", "Partner API", "Inbound Webhook"].join("\n"),
		},
		{ fieldname: "endpoint", label: __("Endpoint"), fieldtype: "Data" },
		{ fieldname: "source", label: __("Source"), fieldtype: "Data" },
	],
};
