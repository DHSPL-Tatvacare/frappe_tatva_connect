// The row's seeded description is painted over the section's subtitle, so it reads as help text rather than as a field a user might try to edit.
frappe.ui.form.on("CRM Tatva Automation", {
	refresh(frm) {
		paint_description(frm);
		// The search toggle owns a full-text index; a rebuild is offered on its own form so a drifted index can be recovered without the console.
		if (frm.doc.name === "Search::Index::indexing") {
			frm.add_custom_button(__("Rebuild Search Index"), () => {
				frappe.call({ method: "tatva_connect.search.api.rebuild_index", freeze: true }).then(() => {
					frappe.show_alert({ message: __("Search index rebuild queued"), indicator: "green" });
				});
			});
		}
	},
});

function paint_description(frm) {
	if (!frm.doc.description) return;
	// The wrapper exists only because the section carries a static description in its schema — section.js builds it at make time and never again.
	const section = (frm.layout.sections || []).find((s) => s.df && s.df.fieldname === "section_what");
	if (!section || !section.description_wrapper) return;
	const html = frappe.utils.escape_html(frm.doc.description).replace(/\n/g, "<br>");
	section.description_wrapper.html(html);
}
