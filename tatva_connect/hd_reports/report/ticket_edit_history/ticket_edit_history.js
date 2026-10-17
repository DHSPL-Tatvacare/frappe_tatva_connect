frappe.query_reports["Ticket Edit History"] = {
	filters: [
		{ fieldname: "ticket", label: __("Ticket"), fieldtype: "Link", options: "HD Ticket", reqd: 1 },
		{ fieldname: "field", label: __("Field contains"), fieldtype: "Data" },
	],
};
