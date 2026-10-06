// On submit, a phone number already on record makes a lead form ask Yes/No and a ticket form inform; advisory, so a failed check submits.
frappe.web_form.events.on("after_load", function () {
	const form = frappe.web_form;
	if (!form.get_field("__PHONE_FIELD__")) return;

	form.add_submit_step(function () {
		const phone = form.get_value("__PHONE_FIELD__");
		if (!phone) return true;
		// Missing answers are reported by frappe's own check before a lookup is spent on a form that cannot submit yet.
		if (!form.get_values(form.allow_incomplete)) return false;
		return new Promise((resolve) => {
			frappe.call({
				method: "tatva_connect.intake.api.check_existing_patient",
				args: { web_form: form.name, phone: phone },
				freeze: true,
				// website.js shows a refusal's message unless it has somewhere else to go; the rate limit is not the visitor's to read.
				error_msg: $("<div>"),
				always: function (r) {
					const answer = (r && !r.exc && r.message) || {};
					if (!answer.exists) return resolve(true);
					// frappe.confirm and msgprint render HTML, so the server's text is escaped whatever it holds.
					const text = frappe.utils.escape_html(answer.message);
					// Yes submits; No, or closing the dialog, leaves every answer exactly as typed.
					if (answer.ask) return frappe.confirm(text, () => resolve(true), () => resolve(false));
					// The notice belongs to the submit step: the form goes once it is closed, through frappe's one-shot close hook (messages.js).
					frappe.msgprint(text).custom_onhide = () => resolve(true);
				},
			});
		});
	});
});
