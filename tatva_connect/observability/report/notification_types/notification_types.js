frappe.query_reports["Notification Types"] = {
	filters: [
		{ fieldname: "days", label: __("Days"), fieldtype: "Int", default: 7, reqd: 1 },
	],
};
