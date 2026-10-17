// Dashboard Chart Source: Reps by Grain, registered for chart_type=Custom charts.
frappe.provide("frappe.dashboards.chart_sources");

frappe.dashboards.chart_sources["Reps by Grain"] = {
	method: "tatva_connect.crm_setup.dashboard_chart_source.reps_by_grain.reps_by_grain.get",
	filters: [],
};
