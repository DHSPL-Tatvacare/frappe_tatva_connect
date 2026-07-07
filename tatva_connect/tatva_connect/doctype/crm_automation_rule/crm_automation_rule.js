// CRM Automation Rule - native Desk builder. Renders entirely from the server "describe" contract
// (automation.describe): the criterion Field dropdown, its valid Operators, the watch_field dropdown,
// and the value hint all derive from the server - nothing about any field/type is hardcoded here.
// The server validate() is the real guardrail; this is the flow-based UX layer.
//
// Trigger-aware: Task-Completed reads the activity-form schema (activity_fields); Field-Changed
// reads the watched doctype's meta (watch_fields). The describe call carries both; the JS picks by
// trigger_type. changed_from_to is offered only on Field-Changed rules, only on the watch_field.

const ALL_OPERATORS = "=\n!=\n<\n>\n<=\n>=\nlike\nnot like\nin\nnot in\nis set\nis unset\nbetween";

frappe.ui.form.on("CRM Automation Rule", {
	refresh: reload_describe,
	trigger_type: reload_describe,
	task_type: reload_describe,
	watch_doctype: reload_describe,
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
	from_value: render_preview,
	value: render_preview,
	criteria_remove: render_preview,
});

frappe.ui.form.on("CRM Automation Action", {
	action_type: render_preview,
	task_type: render_preview,
	fieldname: render_preview,
	value_mode: render_preview,
	value: render_preview,
	context_field: render_preview,
	expression: render_preview,
	due_mode: render_preview,
	due_from: render_preview,
	due_expression: render_preview,
	comment_mode: render_preview,
	comment_text: render_preview,
	comment_expression: render_preview,
	child_table: render_preview,
	webhook_endpoint: render_preview,
	actions_remove: render_preview,
});

// -- the describe contract ---------------------------------------------------

function reload_describe(frm) {
	// Task-Completed needs a task_type; Field-Changed needs a watch_doctype. Either loads the
	// describe contract for the active trigger's vocabulary + the (grain-scoped) set-field targets.
	const has_subject = frm.doc.trigger_type === "Field Changed" ? frm.doc.watch_doctype : frm.doc.task_type;
	if (!has_subject) {
		frm._describe = null;
		apply_field_options(frm);
		return render_preview(frm);
	}
	frappe
		.call({
			method: "tatva_connect.automation.describe.describe",
			args: {
				task_type: frm.doc.task_type,
				watch_doctype: frm.doc.watch_doctype,
				vertical: frm.doc.vertical,
				group: frm.doc.group,
				program: frm.doc.program,
			},
		})
		.then((r) => {
			frm._describe = r.message || { activity_fields: [], watch_fields: [], set_field_targets: [] };
			apply_field_options(frm);
			render_preview(frm);
		});
}

function criteria_fields(frm) {
	// The criteria vocabulary picks by trigger: Task-Completed -> activity-form schema;
	// Field-Changed -> the watched doctype's meta fields (watch_fields).
	if (!frm._describe) return [];
	return frm.doc.trigger_type === "Field Changed" ? frm._describe.watch_fields || [] : frm._describe.activity_fields || [];
}

function field_descriptor(frm, key) {
	return criteria_fields(frm).find((f) => f.key === key) || null;
}

function apply_field_options(frm) {
	if (frm.fields_dict.criteria) {
		// The criterion Field dropdown = the active trigger's vocabulary.
		const opts = [""].concat(criteria_fields(frm).map((f) => f.key)).join("\n");
		frm.fields_dict.criteria.grid.update_docfield_property("field", "options", opts);
		(frm.doc.criteria || []).forEach((c) => apply_criterion_row(frm, c.doctype, c.name));
		frm.fields_dict.criteria.grid.refresh();
	}
	if (frm.fields_dict.watch_field && frm._describe) {
		// The watch_field dropdown = the enabled Watchable rows for the chosen doctype.
		const wopts = [""].concat((frm._describe.watch_fields || []).map((f) => f.key)).join("\n");
		frm.fields_dict.watch_field.set_options(wopts.split("\n").filter(Boolean));
	}
}

// Narrow ONE criterion row to its chosen field: valid operators for the field's type, and a value
// hint of the pickable options (Select) - derived, never hardcoded. `changed_from_to` is offered
// only when the criterion field IS the rule's watch_field (the only field with a before/after pair).
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
	let ops = d.operators.slice();
	// changed_from_to is legal only on the watched field of a Field-Changed rule.
	if (frm.doc.trigger_type === "Field Changed" && c.field === frm.doc.watch_field) {
		ops = ops.concat(["changed_from_to"]);
	}
	op_df.options = ops.join("\n");
	if (c.operator && !ops.includes(c.operator)) {
		frappe.model.set_value(cdt, cdn, "operator", "");
	}
	// escape_html: options come from free-text config and land in set_description (raw HTML) - escape.
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
		case "Create Task": {
			const due = a.due_mode === "Expression"
				? ` due <code>${frappe.utils.escape_html(a.due_expression || "?")}</code>`
				: (a.due_from ? ` due from <code>${frappe.utils.escape_html(a.due_from)}</code>` : "");
			return `create a <b>${frappe.utils.escape_html(a.task_type || "?")}</b> task${due}`;
		}
		case "Set Field": {
			let src;
			if (a.value_mode === "From Context") src = `context.${a.context_field || "?"}`;
			else if (a.value_mode === "Expression") src = `<code>${frappe.utils.escape_html(a.expression || "?")}</code>`;
			else src = `"${a.value || ""}"`;
			return `set <b>${frappe.utils.escape_html(a.fieldname || "?")}</b> on ${frappe.utils.escape_html(a.target_doctype || "?")} to ${src}`;
		}
		case "Append Child Row":
			return `append a row to <b>${frappe.utils.escape_html(a.child_table || "?")}</b>`;
		case "Upsert Child Row":
			return `upsert a row in <b>${frappe.utils.escape_html(a.child_table || "?")}</b>`;
		case "Call Webhook":
			return `call webhook <b>${frappe.utils.escape_html(a.webhook_endpoint || "?")}</b>`;
		case "Add Comment": {
			const txt = a.comment_mode === "Expression"
				? `<code>${frappe.utils.escape_html(a.comment_expression || "?")}</code>`
				: `"${frappe.utils.escape_html(a.comment_text || "")}"`;
			return `add a comment ${txt} on the subject`;
		}
		default:
			return "(choose an action)";
	}
}

function render_preview(frm) {
	if (!frm.doc) return;
	const is_fc = frm.doc.trigger_type === "Field Changed";
	const trigger_phrase = is_fc
		? `<b>${frappe.utils.escape_html(frm.doc.watch_doctype || "?")}.${frappe.utils.escape_html(frm.doc.watch_field || "?")}</b> changes`
		: `a <b>${frappe.utils.escape_html(frm.doc.task_type || "?")}</b> task is completed`;
	const criteria = (frm.doc.criteria || [])
		.filter((c) => c.field)
		.map((c) => {
			if (c.operator === "changed_from_to") {
				return `<code>${frappe.utils.escape_html(c.field)}</code> changed from <code>${frappe.utils.escape_html(c.from_value || "")}</code> to <code>${frappe.utils.escape_html(c.value || "")}</code>`;
			}
			return `<code>${frappe.utils.escape_html(c.field)}</code> ${frappe.utils.escape_html(c.operator || "=")} <code>${frappe.utils.escape_html(c.value || "")}</code>`;
		});
	const when = criteria.length ? ` and <i>${criteria.join(" &amp; ")}</i> (all must match)` : "";
	const actions = (frm.doc.actions || []).map((a) => `<li>${action_phrase(a)}</li>`).join("");

	const html = `
		<div style="padding:4px 0;">
			<b>When</b> ${trigger_phrase}
			<span style="color:var(--text-muted)">(${frappe.utils.escape_html(grain_phrase(frm))})</span>${when}
			${actions ? `<b> → then:</b><ul style="margin:4px 0 0 16px;">${actions}</ul>` : `<b> → then:</b> <span style="color:var(--text-muted)">no actions yet</span>`}
		</div>`;
	frm.dashboard.clear_headline();
	frm.dashboard.set_headline(html);
}
