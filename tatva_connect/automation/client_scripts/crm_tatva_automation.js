// The row's seeded description is painted over the section's subtitle, so it reads as help text rather than as a field a user might try to edit.
frappe.ui.form.on("CRM Tatva Automation", {
	refresh(frm) {
		if (!frm.doc.description) return;
		// The wrapper exists only because the section carries a static description in its schema — section.js builds it at make time and never again.
		const section = (frm.layout.sections || []).find((s) => s.df && s.df.fieldname === "section_what");
		if (!section || !section.description_wrapper) return;
		const html = frappe.utils.escape_html(frm.doc.description).replace(/\n/g, "<br>");
		section.description_wrapper.html(html);
	},
});
