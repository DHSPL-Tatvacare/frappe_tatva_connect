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

	// The number we last warned about: retyping the same one must not nag again. A Phone control is
	// a Data control (ControlPhone extends ControlData), so this fires on a debounced keystroke as
	// well as on blur — the same number must not ask twice.
	let warned = "";

	form.on("__PHONE_FIELD__", function () {
		const value = form.get_value("__PHONE_FIELD__") || "";
		if (!value || value === warned) return;
		frappe.call({
			method: "tatva_connect.intake.api.check_existing_patient",
			args: { web_form: form.name, phone: value },
			callback: function (r) {
				const answer = (r && r.message) || {};
				if (!answer.exists) return;
				warned = value;
				// frappe.confirm appends its message as HTML (messages.js), and this one carries a
				// group name an operator typed — escaped with frappe's own helper, never interpolated raw.
				const text = frappe.utils.escape_html(answer.message);
				// No = they should not carry on under this number, so it is cleared (it is the one
				// mandatory question, so the form cannot be submitted until they decide).
				frappe.confirm(text, null, function () {
					phone.set_value("");
					warned = "";
				});
			},
		});
	});
});
