frappe.query_reports["Lead Distribution"] = {
	filters: [{ fieldname: "lead", label: __("Lead"), fieldtype: "Link", options: "CRM Lead", reqd: 1 }],
};
