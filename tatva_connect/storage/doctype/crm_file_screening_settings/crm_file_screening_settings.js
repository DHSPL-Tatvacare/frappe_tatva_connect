// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

// What this form controls, said once in frappe's permanent headline so it reads before the first field.
frappe.ui.form.on("CRM File Screening Settings", {
	refresh(frm) {
		// frappe appends a headline on every call, so the last one is removed first, as its own set_intro does.
		if (frm.__headline) frm.__headline.remove();
		frm.__headline = frm.dashboard.set_headline_alert(
			__("These settings shape the check each uploaded file goes through before it is saved. It runs only when the Storage::File::screening feature flag is on, and only for the channels listed below. A blank field uses the default the code holds."),
			"blue",
			true
		);
	},
});
