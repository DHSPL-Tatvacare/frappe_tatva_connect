// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

// What this form controls, said once in frappe's permanent headline so it reads before the first field.
frappe.ui.form.on("CRM Maps Settings", {
	refresh(frm) {
		// frappe appends a headline on every call, so the last one is removed first, as its own set_intro does.
		if (frm.__headline) frm.__headline.remove();
		frm.__headline = frm.dashboard.set_headline_alert(
			__("This page sets up maps and address search for the CRM. It holds the Google keys and picks which service draws each map. It also lists the lead groups whose clinic visits are checked by distance. Those checks run only while the Location::Google::capture feature flag is on."),
			"blue",
			true
		);
	},
});
