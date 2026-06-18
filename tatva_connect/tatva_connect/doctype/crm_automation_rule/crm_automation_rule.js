// CRM Automation Rule — native Desk builder. Renders entirely from the server "describe" contract
// (automation.describe): the criterion Field dropdown, its valid Operators, and the value hint all
// derive from the chosen task type's activity schema — nothing about any field/type is hardcoded
// here. The server validate() is the real guardrail; this is the flow-based UX layer.

const ALL_OPERATORS = "=\n!=\n<\n>\n<=\n>=\nlike\nnot like\nin\nnot in\nis set\nis unset\nbetween";

frappe.ui.form.on("CRM Automation Rule", {
	refresh: reload_describe,
	task_type: reload_describe,
	vertical: reload_describe,
	group: reload_describe,
	program: reload_describe,
});

frappe.ui.form.on("CRM Automation Criterion", {
	field(frm, cdt, cdn) {
		apply_criterion_row(frm, cdt, cdn);
		frm.fields_dict.criteria.grid.refresh();
		render_preview(frm);
	},
	operator: render_preview,
	value: render_preview,
	criteria_remove: render_preview,
});

frappe.ui.form.on("CRM Automation Action", {
	action_type: render_preview,
	task_type: render_preview,
	fieldname: render_preview,
	value: render_preview,
	context_field: render_preview,
	child_table: render_preview,
	webhook_endpoint: render_preview,
	actions_remove: render_preview,
});

// -- the describe contract ---------------------------------------------------

function reload_describe(frm) {
	if (!frm.doc.task_type) {
		frm._describe = null;
		return render_preview(frm);
	}
	frappe
		.call({
			method: "tatva_connect.automation.describe.describe",
			args: { task_type: frm.doc.task_type, vertical: frm.doc.vertical, group: frm.doc.group, program: frm.doc.program },
		})
		.then((r) => {
			frm._describe = r.message || { activity_fields: [], set_field_targets: [] };
			apply_field_options(frm);
			render_preview(frm);
		});
}

function activity_fields(frm) {
	return (frm._describe && frm._describe.activity_fields) || [];
}

function field_descriptor(frm, key) {
	return activity_fields(frm).find((f) => f.key === key) || null;
}

function apply_field_options(frm) {
	if (!frm.fields_dict.criteria) return;
	// The criterion Field dropdown = every field on this task type's activity form.
	const opts = [""].concat(activity_fields(frm).map((f) => f.key)).join("\n");
	frm.fields_dict.criteria.grid.update_docfield_property("field", "options", opts);
	(frm.doc.criteria || []).forEach((c) => apply_criterion_row(frm, c.doctype, c.name));
	frm.fields_dict.criteria.grid.refresh();
}

// Narrow ONE criterion row to its chosen field: valid operators for the field's type, and a value
// hint of the pickable options (Select) — derived, never hardcoded.
function apply_criterion_row(frm, cdt, cdn) {
	const c = locals[cdt] && locals[cdt][cdn];
	const d = c && field_descriptor(frm, c.field);
	const op_df = frappe.meta.get_docfield(cdt, "operator", cdn);
	const val_df = frappe.meta.get_docfield(cdt, "value", cdn);
	if (!d) {
		op_df.options = ALL_OPERATORS;
		val_df.description = "";
		return;
	}
	op_df.options = d.operators.join("\n");
	if (c.operator && !d.operators.includes(c.operator)) {
		frappe.model.set_value(cdt, cdn, "operator", "");
	}
	// escape_html: options come from CRM Task Type Field.options (free text) and land in
	// set_description, which renders raw HTML — escape to stop stored XSS at the rule author.
	if (Array.isArray(d.options)) {
		val_df.description = __("one of: {0}", [d.options.map(frappe.utils.escape_html).join(", ")]);
	} else if (d.type === "Datetime") {
		val_df.description = __("a date / time value");
	} else if (d.type === "Link") {
		val_df.description = __("a {0} record", [frappe.utils.escape_html(d.options || "linked")]);
	} else {
		val_df.description = "";
	}
}

// -- live plain-English preview ---------------------------------------------

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
