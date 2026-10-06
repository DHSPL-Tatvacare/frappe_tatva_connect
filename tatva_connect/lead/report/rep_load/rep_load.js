frappe.query_reports["Rep Load"] = {
	filters: [
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", reqd: 1, default: frappe.datetime.add_days(frappe.datetime.get_today(), -6) },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", reqd: 1, default: frappe.datetime.get_today() },
	],
};
