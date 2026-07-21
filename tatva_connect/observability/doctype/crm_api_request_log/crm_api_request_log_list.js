// The list must answer "did this call fail, and why" without opening a row — an operator scanning a
// partner incident should never have to click. Mirrors core's error_log_list.js: the indicator returns
// [label, colour, filter], so the badge itself is the click-through into the matching filter.
frappe.listview_settings["CRM API Request Log"] = {
	add_fields: ["is_error", "error_code"],
	get_indicator: function (doc) {
		// is_error, not status_code: a bulk call answers 200 by contract even when every record failed.
		if (cint(doc.is_error)) {
			return [doc.error_code || __("Error"), "red", "is_error,=,1"];
		}
		return [__("OK"), "green", "is_error,=,0"];
	},
	order_by: "creation desc",
};
