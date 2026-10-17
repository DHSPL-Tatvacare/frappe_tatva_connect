// The handbook's onboarding checklist for one or many users, one row per user and grain.
const TATVA_ONBOARDING_COLOUR = { Done: "green", Missing: "red", "Not needed": "gray", "User's own": "orange", Ready: "green", "Not ready": "red" };

frappe.query_reports["User Onboarding"] = {
	filters: [
		{
			fieldname: "users", label: __("Users"), fieldtype: "MultiSelectList", reqd: 1,
			get_data: (txt) => frappe.db.get_link_options("User", txt, { enabled: 1, user_type: "System User" }),
		},
		{ fieldname: "vertical", label: __("Vertical"), fieldtype: "Link", options: "CRM Vertical" },
		{ fieldname: "crm_group", label: __("Group"), fieldtype: "Link", options: "CRM Group" },
		{ fieldname: "program", label: __("Program"), fieldtype: "Link", options: "CRM Program" },
	],
	formatter(value, row, column, data, default_formatter) {
		const colour = TATVA_ONBOARDING_COLOUR[value];
		if (!colour || column.fieldname === "missing") return default_formatter(value, row, column, data);
		return `<span class="indicator-pill ${colour}">${__(value)}</span>`;
	},
};
