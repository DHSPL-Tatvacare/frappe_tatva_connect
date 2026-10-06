frappe.query_reports["Lead Timeline"] = {
	filters: [{ fieldname: "lead", label: __("Lead"), fieldtype: "Link", options: "CRM Lead", reqd: 1 }],
};
