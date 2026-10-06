frappe.query_reports["Lead Visibility Check"] = {
	filters: [
		{ fieldname: "lead", label: __("Lead"), fieldtype: "Link", options: "CRM Lead", reqd: 1 },
		{ fieldname: "user", label: __("User"), fieldtype: "Link", options: "User", reqd: 1 },
	],
};
