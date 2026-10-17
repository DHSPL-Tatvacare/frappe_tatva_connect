frappe.query_reports["Ticket Visibility Check"] = {
	filters: [
		{ fieldname: "ticket", label: __("Ticket"), fieldtype: "Link", options: "HD Ticket", reqd: 1 },
		{ fieldname: "user", label: __("User"), fieldtype: "Link", options: "User", reqd: 1 },
	],
};
