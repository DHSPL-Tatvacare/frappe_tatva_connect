// Dashboard Chart Source: Inbound Leads
// Registered for chart_type=Custom charts. read_config() serves this file; the Desk evals it
// to register the source's data method + the filter fields shown in the chart config UI.
frappe.provide("frappe.dashboards.chart_sources");

frappe.dashboards.chart_sources["Inbound Leads"] = {
	method: "tatva_connect.observability.dashboard_chart_source.inbound_leads.inbound_leads.get",
	filters: [
		{ fieldname: "days", label: __("Days"), fieldtype: "Int", default: 30 },
	],
};
