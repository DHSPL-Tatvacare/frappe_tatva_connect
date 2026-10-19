frappe.query_reports["Integration Endpoint Breakdown"] = {
	filters: [
		{ fieldname: "from_datetime", label: __("From"), fieldtype: "Datetime", reqd: 1, default: frappe.datetime.add_days(frappe.datetime.now_datetime(), -1) },
		{ fieldname: "integration", label: __("Integration"), fieldtype: "Data", description: __("Blank for all. e.g. partner-api-gfis@tatvacare.in, WhatsApp") },
		{ fieldname: "lane", label: __("Lane"), fieldtype: "Select", options: "\nPartner API\nInbound Webhook\nMCP" },
	],
};
