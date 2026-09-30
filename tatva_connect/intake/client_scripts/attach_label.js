// Every intake form: an uploaded file is shown by its name, never its storage URL. Safe to run twice.
frappe.web_form.events.on("after_load", function () {
	const form = frappe.web_form;

	// frappe prints the raw file_url (attach.js:112), unreadable for an offloaded blob; `form.fields` are definitions, only get_field returns a control.
	const attachNames = form.fields
		.filter((f) => f && (f.fieldtype === "Attach" || f.fieldtype === "Attach Image"))
		.map((f) => f.fieldname);

	function relabelAttachments() {
		attachNames.forEach(function (fieldname) {
			const field = form.get_field(fieldname);
			const url = (field && field.value) || "";
			// `.ellipsis a` is the file link in BOTH of attach.js's branches; a bare "a" also matches Reload/Clear, which then read as the file name and, sitting outside .ellipsis, never truncate.
			const link = field && field.$wrapper && field.$wrapper.find(".ellipsis a");
			if (!url || !link || !link.length) return;
			// The key lives in the QUERY, so it is read before the query is dropped; a local /files/x.pdf has none and falls through to its own basename.
			const tail = decodeURIComponent(url.split("file_name=").pop().split("&")[0].split("/").pop());
			link.text(tail.replace(/^[0-9a-f]{6,}_/, "") || url);
		});
	}

	attachNames.forEach((fieldname) => form.on(fieldname, relabelAttachments));
	relabelAttachments();
});
