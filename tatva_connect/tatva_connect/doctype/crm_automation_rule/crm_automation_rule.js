// CRM Automation Rule — Desk builder polish (spec §7): a live plain-English preview of the rule and
// an available-fields hint, so authoring stays flow-based inside the native form. The doctype's
// validate() is the real guardrail; this is UX only.

frappe.ui.form.on("CRM Automation Rule", {
	refresh(frm) {
		render_preview(frm);
		load_schema_hint(frm);
	},
	task_type(frm) {
		frm._schema_fields = null;
		load_schema_hint(frm);
		render_preview(frm);
	},
	trigger_type: render_preview,
	vertical: render_preview,
	group: render_preview,
	program: render_preview,
});

["criteria", "actions"].forEach((table) => {
	frappe.ui.form.on(table === "criteria" ? "CRM Automation Criterion" : "CRM Automation Action", {
		[`${table}_remove`]: render_preview,
		field: render_preview,
		operator: render_preview,
		value: render_preview,
		action_type: render_preview,
		task_type: render_preview,
		fieldname: render_preview,
		child_table: render_preview,
	});
});

function grain_phrase(frm) {
	const parts = ["vertical", "group", "program"].map((a) => frm.doc[a]).filter(Boolean);
	return parts.length ? parts.join(" · ") : "no grain set";
}

function action_phrase(a) {
	switch (a.action_type) {
		case "Create Task":
			return `create a <b>${frappe.utils.escape_html(a.task_type || "?")}</b> task`;
		case "Set Field": {
			const src = a.value_mode === "From Context" ? `context.${a.context_field || "?"}` : `"${a.value || ""}"`;
			return `set <b>${frappe.utils.escape_html(a.fieldname || "?")}</b> on ${frappe.utils.escape_html(a.target_doctype || "?")} to ${frappe.utils.escape_html(src)}`;
		}
		case "Append Child Row":
			return `append a row to <b>${frappe.utils.escape_html(a.child_table || "?")}</b>`;
		case "Upsert Child Row":
			return `upsert a row in <b>${frappe.utils.escape_html(a.child_table || "?")}</b>`;
		case "Call Webhook":
			return `call webhook <b>${frappe.utils.escape_html(a.webhook_endpoint || "?")}</b>`;
		default:
			return "(choose an action)";
	}
}

function render_preview(frm) {
	if (!frm.doc) return;
	const tt = frm.doc.task_type || "?";
	const criteria = (frm.doc.criteria || [])
		.filter((c) => c.field)
		.map((c) => `<code>${frappe.utils.escape_html(c.field)}</code> ${frappe.utils.escape_html(c.operator || "=")} <code>${frappe.utils.escape_html(c.value || "")}</code>`);
	const when = criteria.length ? ` and <i>${criteria.join(" &amp; ")}</i> (all must match)` : "";
	const actions = (frm.doc.actions || []).map((a) => `<li>${action_phrase(a)}</li>`).join("");

	const html = `
		<div style="padding:4px 0;">
			<b>When</b> a <b>${frappe.utils.escape_html(tt)}</b> task is completed
			<span style="color:var(--text-muted)">(${frappe.utils.escape_html(grain_phrase(frm))})</span>${when}
			${actions ? `<b> → then:</b><ul style="margin:4px 0 0 16px;">${actions}</ul>` : `<b> → then:</b> <span style="color:var(--text-muted)">no actions yet</span>`}
		</div>`;
	frm.dashboard.clear_headline();
	frm.dashboard.set_headline(html);
}

function load_schema_hint(frm) {
	if (!frm.doc.task_type) return;
	if (frm._schema_fields) return show_schema_hint(frm);
	frappe.db
		.get_list("CRM Task Type Field", {
			filters: { parent: frm.doc.task_type, parenttype: "CRM Task Type" },
			fields: ["fieldname"],
			limit: 0,
			parent: "CRM Task Type",
		})
		.then((rows) => {
			frm._schema_fields = (rows || []).map((r) => r.fieldname).filter(Boolean);
			show_schema_hint(frm);
		});
}

function show_schema_hint(frm) {
	if (!frm._schema_fields || !frm._schema_fields.length) return;
	const fields = frm._schema_fields.map((f) => `<code>${frappe.utils.escape_html(f)}</code>`).join(", ");
	frm.dashboard.add_comment(
		__("Activity fields available for criteria / context: {0}", [fields]),
		"blue",
		true
	);
}
