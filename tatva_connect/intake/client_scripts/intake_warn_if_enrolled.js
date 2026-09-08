// Warn that the number just typed is ALREADY a lead on this form's line, and let the person decide.
// Appended to the published form's client script by the builder when the contract ticks
// `warn_if_already_enrolled`; `__PHONE_FIELD__` is the contract's own lead -> mobile_no question,
// substituted at sync time, so nothing here is hardcoded to one form.
//
// It runs on the phone field's CHANGE event, the same native seam the picker cascades use, and NOT
// on submit: a public form's `frappe.call` is website.js's lightweight one, which has no
// error-handler seam, so a refusal raised at submit reaches the visitor as a dead-end msgprint.
// The submit path is untouched — a slow, failed or refused check leaves the form exactly as it is
// today. Advisory, never a gate; the server decides nothing here and answers only yes/no.
frappe.web_form.events.on("after_load", function () {
	const form = frappe.web_form;
	const phone = form.get_field("__PHONE_FIELD__");
	if (!phone) return;

	// TWO guards, because one edit fires TWO events. A Phone control is a Data control
	// (ControlPhone extends ControlData), which binds `change` AND a 500ms-debounced `input`, so
	// typing then leaving the field asks twice.
	//   `warned`   — the number a dialog was already shown for, so retyping it does not nag.
	//   `inFlight` — a check is on the wire RIGHT NOW. This is the one that matters: `warned` is set
	//                when the ANSWER arrives, so without it both events leave before either reply
	//                lands, both pass, and the visitor gets two dialogs for one number.
	// It is cleared on `always`, never in the callback: the callback does not run on a failed or
	// rate-limited request, and a flag stuck at true would silently kill the warning for the session.
	let warned = "";
	let inFlight = false;

	form.on("__PHONE_FIELD__", function () {
		const value = form.get_value("__PHONE_FIELD__") || "";
		if (!value || value === warned || inFlight) return;
		inFlight = true;
		const req = frappe.call({
			method: "tatva_connect.intake.api.check_existing_patient",
			args: { web_form: form.name, phone: value },
			callback: function (r) {
				const answer = (r && r.message) || {};
				if (!answer.exists) return;
				warned = value;
				// frappe.confirm appends its message as HTML (messages.js). The server composes the
				// whole string from a fixed literal today, so escaping is belt-and-braces — kept
				// because the sanitising must not depend on the server never interpolating again.
				const text = frappe.utils.escape_html(answer.message);
				// No = they should not carry on under this number, so it is cleared (it is the one
				// mandatory question, so the form cannot be submitted until they decide).
				frappe.confirm(text, null, function () {
					// No: clear the number AND forget it, so retyping it is warned about again.
					phone.set_value("");
					warned = "";
				});
			},
		});
		// website.js's `frappe.call` returns the jQuery promise but honours no `always` option of its
		// own, so the release is chained onto what it returns.
		if (req && req.always) req.always(function () { inFlight = false; });
	});
});
