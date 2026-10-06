frappe.query_reports["Lead Edit History"] = {
	filters: [
		{ fieldname: "lead", label: __("Lead"), fieldtype: "Link", options: "CRM Lead", reqd: 1 },
		{ fieldname: "field", label: __("Field contains"), fieldtype: "Data" },
	],
};
