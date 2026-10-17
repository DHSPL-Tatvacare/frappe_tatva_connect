frappe.query_reports["Ticket Timeline"] = {
	filters: [{ fieldname: "ticket", label: __("Ticket"), fieldtype: "Link", options: "HD Ticket", reqd: 1 }],
};
