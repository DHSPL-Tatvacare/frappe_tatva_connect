frappe.query_reports["Overloaded Contacts"] = {
	filters: [
		{ fieldname: "min_count", label: __("Minimum Count"), fieldtype: "Int", default: 2 },
		{ fieldname: "which", label: __("Which"), fieldtype: "Select", options: "Either\nEmails\nPhones", default: "Either" },
	],
};
