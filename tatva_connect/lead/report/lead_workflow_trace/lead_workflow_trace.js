frappe.query_reports["Lead Workflow Trace"] = {
	filters: [{ fieldname: "lead", label: __("Lead"), fieldtype: "Link", options: "CRM Lead", reqd: 1 }],
};
