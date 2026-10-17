frappe.query_reports["Contact Ticket History"] = {
	filters: [{ fieldname: "contact", label: __("Contact"), fieldtype: "Link", options: "Contact", reqd: 1 }],
};
