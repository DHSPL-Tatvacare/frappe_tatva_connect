// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

// Retiring a derived field is not a private edit — it takes a column out of every saved view that names
// it, on every rep's screen. So the form says how many, always, and asks before the switch goes off.
// Deleting goes through frappe's own permanent-delete confirmation with the same count on screen.
// Nothing is repaired here: each rep's view is cleaned up on their own next load, by the server.

frappe.ui.form.on("CRM Derived Field", {
	refresh(frm) {
		// A fresh render is a fresh decision: whatever was confirmed on the last save does not carry over.
		frm.__enabled_before = frm.doc.enabled;
		frm.__retire_confirmed = false;
		show_usage(frm);
	},

	validate(frm) {
		if (frm.__retire_confirmed || !retiring(frm) || !in_use(frm)) return;
		frappe.validated = false;
		frappe.confirm(consequence(frm), () => {
			frm.__retire_confirmed = true;
			frm.save();
		});
	},
});

// Turning the switch off is the retirement; every other edit leaves the field where it is.
const retiring = (frm) => frm.__enabled_before && !frm.doc.enabled;

const in_use = (frm) => (frm.__usage || {}).views > 0;

function consequence(frm) {
	const usage = frm.__usage || {};
	return __(
		"{0} is used by {1} saved view(s) across {2} person(s). Retiring it removes the column from those views the next time each person opens them.",
		[frm.doc.label || frm.doc.fieldname, usage.views, usage.users]
	);
}

function show_usage(frm) {
	frm.__usage = null;
	frm.set_intro("");
	if (frm.is_new() || !frm.doc.dt || !frm.doc.fieldname) return;
	frappe
		.call("tatva_connect.tatva_connect.doctype.crm_derived_field.crm_derived_field.usage", {
			dt: frm.doc.dt,
			fieldname: frm.doc.fieldname,
		})
		.then(({ message }) => {
			frm.__usage = message;
			if (!in_use(frm)) return;
			frm.set_intro(consequence(frm), "orange");
		});
}
