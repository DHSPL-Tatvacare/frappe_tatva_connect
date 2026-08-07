// The row's seeded description is painted over the section's subtitle, so it reads as help text rather than as a field a user might try to edit.
// The header pill is painted HERE and not left to frappe: indicator.js:87 consults the list view's get_indicator before its own doc.enabled fallback at :107, and that list read is a page-lifetime cache no form hook fills — so a form reached from the list showed the pill it had when the list was last drawn, not the save just made.
frappe.ui.form.on("CRM Tatva Automation", {
	refresh(frm) {
		paint_description(frm);
		paint_status(frm);
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

// Runs after frappe has already painted the header — form.js orders refresh_header BEFORE script_manager's
// refresh, and says so in its own comment — so what is set here is what the operator ends up reading.
function paint_status(frm) {
	// An unsaved form must keep frappe's own orange "Not Saved"; overwriting it would claim a state the row does not have yet.
	if (frm.is_new() || frm.doc.__unsaved) return;

	// Off is knowable from the row in front of us, so it is painted with no round trip and can never lag a save.
	if (!frm.doc.enabled) {
		frm.page.set_indicator(__("Off"), "gray");
		return;
	}

	// On is the honest optimistic answer while the one thing the form cannot know — whether an ancestor is
	// dormant — is fetched. Never shows the OPPOSITE of the truth, only a less specific version of it.
	frm.page.set_indicator(__("On"), "green");

	const painted_for = frm.doc.name;
	frappe.call({ method: "tatva_connect.automation.status.switch_status", args: { key: frm.doc.name } })
		.then((r) => {
			const row = r && r.message;
			// The operator may have moved on, or saved again, while this was in flight — a late answer must never repaint a different row or a stale tick.
			if (!row || !frm.page || frm.doc.name !== painted_for || Boolean(frm.doc.enabled) !== row.enabled) return;
			if (row.status === "broken_dependency") {
				frm.page.set_indicator(__("Broken dependency"), "red");
			} else if (row.status === "on") {
				frm.page.set_indicator(__("On"), "green");
			} else {
				frm.page.set_indicator(__("Off"), "gray");
			}
		});
}
