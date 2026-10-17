// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

// What this form controls, said once in frappe's permanent headline so it reads before the first field.
frappe.ui.form.on("CRM Intake Settings", {
	refresh(frm) {
		// frappe appends a headline on every call, so the last one is removed first, as its own set_intro does.
		if (frm.__headline) frm.__headline.remove();
		frm.__headline = frm.dashboard.set_headline_alert(
			__("These limits slow down abuse of the public enrolment forms. They cap form submits, lookups and guest file uploads from one source. They also size the hourly clean-up of unused guest files. The limits apply only while the Intake::RateLimit::enforcement feature flag is on."),
			"blue",
			true
		);
	},
});
