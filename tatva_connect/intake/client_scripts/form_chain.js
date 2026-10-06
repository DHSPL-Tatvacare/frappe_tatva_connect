// Every intake form, loaded before any other script: `form.on` adds a change handler, and `form.add_submit_step` adds a submit step; both run in the order added. Safe to run twice.
(function () {
	const form = frappe.web_form;
	if (form.add_submit_step) return;
	const on = form.on;
	const save = form.save;
	const handlers = {};
	const steps = [];
	let submitting = false;

	form.on = function (fieldname, handler) {
		// The first handler takes the field's one native slot (web_form.js `on`), which then runs every handler.
		if (!handlers[fieldname]) {
			handlers[fieldname] = [];
			on.call(form, fieldname, (field, value) => handlers[fieldname].forEach((run) => run(field, value)));
		}
		handlers[fieldname].push(handler);
	};

	// A step returns true (or a promise of it) to go on, false to stop and leave the form exactly as filled.
	form.add_submit_step = (step) => steps.push(step);

	// Returns false at once, as frappe's own save does, so the browser never posts the form itself; a press while a submit is under way is ignored.
	form.save = function () {
		if (submitting || window.saving) return false;
		submitting = true;
		steps
			.reduce((going, step) => going.then((go) => go && step()), Promise.resolve(true))
			.then((go) => go && save.call(form))
			// A step that fails stops the submit and leaves the form as filled; the error is logged, never swallowed.
			.catch((error) => console.error(error))
			.finally(() => (submitting = false));
		return false;
	};
})();
