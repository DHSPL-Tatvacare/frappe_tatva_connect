frappe.query_reports["Rep Load"] = {
	filters: [
		{ fieldname: "pool", label: __("Pool"), fieldtype: "Link", options: "Assignment Rule", get_query: () => ({ filters: { document_type: "CRM Lead" } }) },
		{ fieldname: "date", label: __("Date"), fieldtype: "Date", default: frappe.datetime.get_today(), depends_on: "eval:doc.pool" },
		{
			fieldname: "reps", label: __("Reps"), fieldtype: "MultiSelectList", depends_on: "eval:doc.pool",
			get_data: (txt) => frappe.db.get_link_options("User", txt, { enabled: 1, user_type: "System User" }),
		},
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", reqd: 1, default: frappe.datetime.add_days(frappe.datetime.get_today(), -6), depends_on: "eval:!doc.pool" },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", reqd: 1, default: frappe.datetime.get_today(), depends_on: "eval:!doc.pool" },
	],
};
