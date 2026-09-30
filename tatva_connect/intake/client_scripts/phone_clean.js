// Phone fields only: a pasted number carries an invisible U+202A (WhatsApp) or a second +91, and both make libphonenumber refuse it.
frappe.web_form.events.on("after_load", function () {
	const form = frappe.web_form;
	const INVISIBLE = /[­​-‏‪-‮⁠-⁤⁦-⁩﻿]/g;

	// ControlPhone stores "<isd>-<number>" and renders it only when there is exactly one hyphen (phone.js set_formatted_input).
	function clean(raw, isd) {
		// India only: the picker's own code decides, so a number on any other country code is never touched.
		if (isd !== "+91") return String(raw);
		let digits = String(raw).replace(INVISIBLE, "").replace(/\D/g, "");
		// "0091 98765 43210" — the international prefix written the old way.
		if (digits.startsWith("00")) digits = digits.slice(2);
		// Peel every leading 91 that is not part of the ten-digit number itself.
		while (digits.length > 10 && digits.startsWith("91")) digits = digits.slice(2);
		// A trunk zero copied off a contact card.
		if (digits.length === 11 && digits.startsWith("0")) digits = digits.slice(1);
		// Only a confident ten-digit number is written back; anything else is left exactly as the rep left it.
		return digits.length === 10 ? "+91-" + digits : String(raw);
	}

	form.fields
		.filter((f) => f && f.fieldtype === "Phone")
		.map((f) => f.fieldname)
		.forEach(function (fieldname) {
			form.on(fieldname, function () {
				const field = form.get_field(fieldname);
				const raw = form.get_value(fieldname);
				if (typeof raw !== "string" || !raw) return;
				const isd = (field && field.$isd && field.$isd.text().trim()) || "+91";
				const out = clean(raw, isd);
				if (out !== raw) form.set_value(fieldname, out);
			});
		});
});
