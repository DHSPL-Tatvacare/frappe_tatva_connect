frappe.query_reports["Lead Task Trail"] = {
	filters: [{ fieldname: "lead", label: __("Lead"), fieldtype: "Link", options: "CRM Lead", reqd: 1 }],
};
