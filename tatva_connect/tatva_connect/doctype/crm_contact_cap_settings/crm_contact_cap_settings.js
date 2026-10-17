// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

// What this form controls, said once in frappe's permanent headline so it reads before the first field.
frappe.ui.form.on("CRM Contact Cap Settings", {
	refresh(frm) {
		// frappe appends a headline on every call, so the last one is removed first, as its own set_intro does.
		if (frm.__headline) frm.__headline.remove();
		frm.__headline = frm.dashboard.set_headline_alert(
			__("This form limits how often workflows reach one mobile number. WhatsApp sends and voice calls count toward one shared total. Email does not count. When a number is at the limit, the send fails and the workflow takes its failed branch."),
			"blue",
			true
		);
	},
});
