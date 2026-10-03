// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

frappe.views.calendar["Tatva User Leave"] = {
	field_map: {
		start: "from_date",
		end: "to_date",
		id: "name",
		title: "user",
		allDay: "allDay",
	},
	order_by: "from_date",
	get_events_method: "frappe.desk.calendar.get_events",
};
