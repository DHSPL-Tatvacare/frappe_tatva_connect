frappe.query_reports["Dummy Emails"] = {
	filters: [
		{ fieldname: "patterns", label: __("Dummy Email Patterns"), fieldtype: "Small Text", description: __("One pattern per line, * matches any text. Blank uses the default list.") },
	],
};
