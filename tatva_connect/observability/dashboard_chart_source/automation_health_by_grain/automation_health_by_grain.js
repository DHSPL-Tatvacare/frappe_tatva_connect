// Dashboard Chart Source: Automation Health by Grain
// Registered for chart_type=Custom charts. read_config() serves this file; the Desk evals it
// to register the source's data method + the filter fields shown in the chart config UI.
frappe.provide("frappe.dashboards.chart_sources");

frappe.dashboards.chart_sources["Automation Health by Grain"] = {
	method: "tatva_connect.observability.dashboard_chart_source.automation_health_by_grain.automation_health_by_grain.get",
	filters: [{ fieldname: "days", label: __("Days"), fieldtype: "Int", default: 7 }],
};
