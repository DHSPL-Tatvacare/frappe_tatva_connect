frappe.query_reports["SLA Breaches"] = {
	filters: [
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", reqd: 1, default: frappe.datetime.add_days(frappe.datetime.get_today(), -6) },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", reqd: 1, default: frappe.datetime.get_today() },
		{ fieldname: "team", label: __("Team"), fieldtype: "Link", options: "HD Team" },
		{ fieldname: "ticket_type", label: __("Ticket Type"), fieldtype: "Link", options: "HD Ticket Type" },
		{ fieldname: "priority", label: __("Priority"), fieldtype: "Link", options: "HD Ticket Priority" },
	],
};
