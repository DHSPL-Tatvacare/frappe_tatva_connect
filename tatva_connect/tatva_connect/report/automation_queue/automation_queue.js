// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

frappe.query_reports["Automation Queue"] = {
	filters: [
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			// Blank = every state. "Due now" is derived, not stored — filter on Pending and read the
			// Status column, which reports an elapsed Wait as overdue.
			options: ["", "Pending", "Done", "Failed", "Cancelled"],
		},
		{
			fieldname: "rule",
			label: __("Rule"),
			fieldtype: "Link",
			options: "CRM Automation Rule",
		},
		{
			fieldname: "from_date",
			label: __("Resumes After"),
			fieldtype: "Datetime",
		},
		{
			fieldname: "to_date",
			label: __("Resumes Before"),
			fieldtype: "Datetime",
		},
		{
			fieldname: "limit",
			label: __("Row Limit"),
			fieldtype: "Int",
			default: 500,
		},
	],

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "status" && data) {
			const tone = { [__("Due now")]: "red", Failed: "red", Cancelled: "gray", Done: "green", Pending: "blue" };
			return `<span class="indicator-pill ${tone[data.status] || "gray"}">${value}</span>`;
		}
		return value;
	},
};
