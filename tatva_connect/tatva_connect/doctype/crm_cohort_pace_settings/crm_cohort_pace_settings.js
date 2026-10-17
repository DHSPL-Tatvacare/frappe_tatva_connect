// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

// What this pace governs, said once in frappe's permanent headline so it reads before the first field.
frappe.ui.form.on("CRM Cohort Pace Settings", {
	refresh(frm) {
		// frappe appends a headline on every call, so the last one is removed first, as its own set_intro does.
		if (frm.__headline) frm.__headline.remove();
		frm.__headline = frm.dashboard.set_headline_alert(
			__("Set how fast workflows reach patients. Journeys that a save starts, scheduled campaign starts and journeys waking from a wait all run in batches at this pace. A steady pace stops a burst from flooding the queue. It also keeps messages within the rate your provider accepts."),
			"blue",
			true
		);
	},
});
