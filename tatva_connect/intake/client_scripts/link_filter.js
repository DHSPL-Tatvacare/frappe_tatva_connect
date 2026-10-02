// Dropdowns that follow the rules their target field declares (Sub Type after Ticket Type, disabled rows hidden); the server applies the rules, this only asks.
frappe.web_form.events.on("after_load", function () {
	const form = frappe.web_form;
	// { question: [questions it reads] }, written by the builder from the question's own link_filters.
	const RULES = __LINK_RULES__;

	Object.keys(RULES).forEach(function (fieldname) {
		const field = form.get_field(fieldname);
		if (!field) return;
		const all = (field._data || []).slice();
		let asked = 0;

		function narrow() {
			const values = {};
			RULES[fieldname].forEach((parent) => (values[parent] = form.get_value(parent) || ""));
			const ask = ++asked;
			frappe.call({
				method: "tatva_connect.intake.api.link_options",
				args: { web_form: form.name, fieldname: fieldname, values: values },
				callback: function (r) {
					// Only the newest answer paints: a slow reply to an earlier pick must not overwrite a later one.
					if (ask !== asked) return;
					const allowed = new Set((r && r.message) || []);
					const options = all.filter((o) => allowed.has(o.value));
					field.set_data(options);
					const chosen = form.get_value(fieldname);
					if (chosen && !options.some((o) => o.value === chosen)) field.set_value("");
				},
			});
		}

		RULES[fieldname].forEach((parent) => form.on(parent, narrow));
		narrow();
	});
});
