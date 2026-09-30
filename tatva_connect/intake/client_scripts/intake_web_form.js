// Picker cascades: State -> City and Hospital -> Doctor. UX only, never a gate; the real gate is the in-grain validation on save.
// Reads `_data`, NOT `df.options`: frappe serialises a web-form Link's options to a JSON STRING, so filtering df.options silently does nothing.
// Both pairs are read off the child's OWN composite key, so nothing here restates how a master is named.
frappe.web_form.events.on("after_load", function () {
	const form = frappe.web_form;

	// CRM City is "<City>::<State>" — the parent is the LAST segment.
	// CRM Doctor is "<hospital key>::<Doctor>" and CRM Hospital is that hospital key — the parent is the PREFIX.
	const CASCADES = [
		{ parent: "state", child: "city", belongs: (v, p) => v.split("::").pop() === p },
		{ parent: "hospital", child: "doctor", belongs: (v, p) => v.startsWith(p + "::") },
	];

	CASCADES.forEach(function (rule) {
		const parent = form.get_field(rule.parent);
		const child = form.get_field(rule.child);
		if (!parent || !child) return;

		const all = (child._data || []).slice();

		// "Others" survives every filter: it is how a patient reaches the "not listed" box, so a
		// narrowed list must never be the reason they cannot answer.
		function applyFilter() {
			const chosenParent = form.get_value(rule.parent) || "";
			const options = chosenParent
				? all.filter((o) => o.label === "Others" || rule.belongs(String(o.value || ""), chosenParent))
				: all;
			child.set_data(options);
			const chosen = form.get_value(rule.child);
			if (chosen && !options.some((o) => o.value === chosen)) child.set_value("");
		}

		form.on(rule.parent, applyFilter);
		applyFilter();
	});
});
