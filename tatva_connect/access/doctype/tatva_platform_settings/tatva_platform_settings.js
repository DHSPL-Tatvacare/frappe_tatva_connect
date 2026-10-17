// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

// What these platform rules cover, said once in frappe's permanent headline so it reads before the first field.
frappe.ui.form.on("Tatva Platform Settings", {
	refresh(frm) {
		// frappe appends a headline on every call, so the last one is removed first, as its own set_intro does.
		if (frm.__headline) frm.__headline.remove();
		frm.__headline = frm.dashboard.set_headline_alert(
			__("These rules apply across the whole platform, whichever app sends the request. Today they decide who can be invited. An invitation goes only to an allowed domain, and never to a blocked one. Leave a list blank to turn its check off."),
			"blue",
			true
		);
	},
});
