// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

// What this form controls, said once in frappe's permanent headline so it reads before the first field.
frappe.ui.form.on("CRM MCP Settings", {
	refresh(frm) {
		// frappe appends a headline on every call, so the last one is removed first, as its own set_intro does.
		if (frm.__headline) frm.__headline.remove();
		frm.__headline = frm.dashboard.set_headline_alert(
			__("The documentation server lets staff connect their own AI assistant to ask how to do a task. The assistant reads the handbook, form fields and Desk pages, never records. Turn on the MCP::Docs::server feature flag to start it. This form sets its call limits and answer sizes."),
			"blue",
			true
		);
	},
});
