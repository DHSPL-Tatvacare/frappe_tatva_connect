// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

// How every limit on this form behaves, said once in frappe's permanent headline so it reads before the first field.
frappe.ui.form.on("CRM Partner API Settings", {
	refresh(frm) {
		// frappe appends a headline on every call, so the last one is removed first, as its own set_intro does.
		if (frm.__headline) frm.__headline.remove();
		frm.__headline = frm.dashboard.set_headline_alert(
			__("Every limit on this page is a bucket that refills steadily across its window. A partner can spend a full bucket at once. After that, its calls continue at the refill rate. A refused call gets a Retry-After header that says how long the bucket needs. So a rate of N per day arrives spread evenly through the day."),
			"blue",
			true
		);
	},
});
