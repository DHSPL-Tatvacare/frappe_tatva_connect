// CRM Automation Rule - native Desk builder (v2). Renders When -> If -> Then entirely from the ONE
// server contract (automation.describe.builder_schema): the criterion Field/Operator/Value
// pickers and every action verb's typed params derive from it - nothing about any field, type,
// operator or verb is hardcoded here. This retires the pre-v2 flow (trigger_type/task_type/
// watch_doctype/watch_field - dropped from the doctype itself in Task 1) and the hardcoded
// ALL_OPERATORS literal it used to narrow criteria with.
//
// The server validate() is the real guardrail (it re-derives this SAME builder_schema call and
// rejects any deviation) - this script is the flow-based UX layer only: typed controls via the
// native `grid_row.set_field_property`/`make_control` seam, no innerHTML/v-html/MutationObserver
// into other components (S.4), Criteria + Then stay hidden until On DocType + Event are both set.

frappe.ui.form.on("CRM Automation Rule", {
	refresh: reload_describe,
	on_doctype: reload_describe,
	event: reload_describe,
	vertical: reload_describe,
	group: reload_describe,
	program: reload_describe,
});

frappe.ui.form.on("CRM Automation Criterion", {
	field(frm, cdt, cdn) {
		apply_criterion_row(frm, cdt, cdn);
		render_preview(frm);
	},
	operator: render_preview,
	from_value: render_preview,
	value: render_preview,
	criteria_remove: render_preview,
	form_render(frm, cdt, cdn) {
		// A freshly-opened row form starts with the doctype's default (Data) control - re-apply the
		// typed control the field's descriptor calls for.
		apply_criterion_row(frm, cdt, cdn);
	},
});

frappe.ui.form.on("CRM Automation Action", {
	action_type(frm, cdt, cdn) {
		apply_action_row(frm, cdt, cdn);
		render_preview(frm);
	},
	task_type: render_preview,
	fieldname: render_preview,
	target_doctype: render_preview,
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
	require_fields: render_preview,
	geofence_meters: render_preview,
	wait_expression: render_preview,
	whatsapp_template: render_preview,
	email_recipient: render_preview,
	actions_remove: render_preview,
	form_render(frm, cdt, cdn) {
		apply_action_row(frm, cdt, cdn);
	},
});

// -- the builder contract -----------------------------------------------------

function has_trigger(frm) {
	return !!(frm.doc.on_doctype && frm.doc.event);
}

// Criteria + Then stay disabled until the trigger (On DocType + Event) is complete - the builder's
// narrative control (plan Part G): nothing downstream is offered before its upstream is valid.
function toggle_sections(frm) {
	frm.toggle_display(["criteria", "then_section", "actions"], has_trigger(frm));
}

function reload_describe(frm) {
	toggle_sections(frm);
	if (!has_trigger(frm)) {
		frm._schema = null;
		render_preview(frm);
		return;
	}
	frappe
		.call({
			method: "tatva_connect.automation.describe.builder_schema",
			args: {
				on_doctype: frm.doc.on_doctype,
				event: frm.doc.event,
				vertical: frm.doc.vertical,
				group: frm.doc.group,
				program: frm.doc.program,
			},
		})
		.then((r) => {
			frm._schema = r.message || { fields: [], operators_by_type: {}, verbs: [], set_targets: [] };
			apply_field_options(frm);
			apply_action_query(frm);
			(frm.doc.actions || []).forEach((a) => apply_action_row(frm, a.doctype, a.name));
			render_preview(frm);
		});
}

function field_descriptor(frm, key) {
	return frm._schema ? frm._schema.fields.find((f) => f.key === key) || null : null;
}

// The criterion Field dropdown = builder_schema's `fields` (already can_watch-scoped server-side).
function apply_field_options(frm) {
	if (!frm.fields_dict.criteria) return;
	const opts = [""].concat((frm._schema.fields || []).map((f) => f.key)).join("\n");
	frm.fields_dict.criteria.grid.update_docfield_property("field", "options", opts);
	frm.fields_dict.criteria.grid.refresh();
	(frm.doc.criteria || []).forEach((c) => apply_criterion_row(frm, c.doctype, c.name));
}

// Narrow ONE criterion row to its chosen field: valid Operators for the field's schema type (from
// builder_schema.operators_by_type - the one operator table, no hardcoded literal), the `changed…`
// operators offered only when the rule's Event is Updated, and a typed Value/From Value control
// matching the field's `pick` (Link search / Select options / Date) instead of free text.
function apply_criterion_row(frm, cdt, cdn) {
	const c = locals[cdt] && locals[cdt][cdn];
	if (!c) return;
	const d = field_descriptor(frm, c.field);
	const op_df = frappe.meta.get_docfield(cdt, "operator", cdn);
	let ops = d && frm._schema ? (frm._schema.operators_by_type[d.type] || []).slice() : [];
	if (frm.doc.event !== "Updated") {
		ops = ops.filter((op) => op !== "changed to" && op !== "changed from…to");
	}
	op_df.options = ops.join("\n");
	if (c.operator && !ops.includes(c.operator)) {
		frappe.model.set_value(cdt, cdn, "operator", "");
	}
	apply_typed_control(frm, "criteria", cdn, "value", d);
	apply_typed_control(frm, "criteria", cdn, "from_value", d);
}

// Grain-scope Create Task's task_type Link to this rule's own grain axes (blank axis = wildcard,
// same semantics as the runtime scope check in actions._action_create_task).
function apply_action_query(frm) {
	frm.set_query("task_type", "actions", () => ({
		filters: {
			vertical: ["in", ["", frm.doc.vertical]],
			group: ["in", ["", frm.doc.group]],
			program: ["in", ["", frm.doc.program]],
		},
	}));
}

// Then side: Update Field's `fieldname` picks from builder_schema.set_targets (the grain's can_set
// allowlist) instead of free text.
function apply_action_row(frm, cdt, cdn) {
	if (cdt !== "CRM Automation Action" || !frm._schema) return;
	const a = locals[cdt][cdn];
	if (a.action_type !== "Update Field") return;
	const grid_row = frm.fields_dict.actions.grid.grid_rows_by_docname[cdn];
	if (!grid_row) return;
	const targets = frm._schema.set_targets || [];
	const opts = [""].concat(targets.map((t) => t.key)).join("\n");
	grid_row.set_field_property("fieldname", "fieldtype", targets.length ? "Select" : "Data");
	grid_row.set_field_property("fieldname", "options", opts);
}

// Swap a row control's fieldtype/options to match a typed descriptor's `pick` - the one seam every
// typed value control (criterion Value/From Value) uses. `set_field_property` is the native grid
// seam (frappe/public/js/frappe/form/grid_row.js) - it updates both the compact in-grid control and
// the expanded row-form control and refreshes them; no manual DOM control construction (S.4).
function apply_typed_control(frm, table_fieldname, cdn, fieldname, d) {
	const grid_row = frm.fields_dict[table_fieldname].grid.grid_rows_by_docname[cdn];
	if (!grid_row) return;
	const [fieldtype, options] = control_shape(d);
	grid_row.set_field_property(fieldname, "fieldtype", fieldtype);
	grid_row.set_field_property(fieldname, "options", options);
}

function control_shape(d) {
	if (!d) return ["Data", ""];
	if (d.pick && d.pick.kind === "link") return ["Link", d.pick.target || ""];
	if (d.pick && d.pick.kind === "select") return ["Select", [""].concat(d.pick.options || []).join("\n")];
	if (["Date", "Datetime", "Check", "Int", "Float", "Currency"].includes(d.type)) return [d.type, ""];
	return ["Data", ""];
}

// -- live plain-English preview ---------------------------------------------

const _EVENT_VERB = { Created: "is created", Updated: "is updated", Deleted: "is deleted" };

function grain_phrase(frm) {
	const parts = ["vertical", "group", "program"].map((a) => frm.doc[a]).filter(Boolean);
	return parts.length ? parts.join(" · ") : "no grain set";
}

function criterion_phrase(c) {
	if (!c.field) return null;
	const field = frappe.utils.escape_html(c.field);
	const op = frappe.utils.escape_html(c.operator || "is");
	if (c.operator === "changed from…to") {
		return `<code>${field}</code> changed from <code>${frappe.utils.escape_html(c.from_value || "")}</code> to <code>${frappe.utils.escape_html(c.value || "")}</code>`;
	}
	if (c.operator === "is set" || c.operator === "is not set") {
		return `<code>${field}</code> ${op}`;
	}
	return `<code>${field}</code> ${op} <code>${frappe.utils.escape_html(c.value || "")}</code>`;
}

function action_phrase(a) {
	switch (a.action_type) {
		case "Require Fields":
			return `require <b>${frappe.utils.escape_html(a.require_fields || "?")}</b> to be set (blocks save)`;
		case "Require Location":
			return `require a captured location${a.geofence_meters ? ` within <b>${a.geofence_meters}m</b>` : ""} (blocks save)`;
		case "Create Task": {
			const due = a.due_mode === "Expression"
				? ` due <code>${frappe.utils.escape_html(a.due_expression || "?")}</code>`
				: (a.due_from ? ` due from <code>${frappe.utils.escape_html(a.due_from)}</code>` : "");
			return `create a <b>${frappe.utils.escape_html(a.task_type || "?")}</b> task${due}`;
		}
		case "Update Field": {
			let src;
			if (a.value_mode === "From Context") src = `context.${frappe.utils.escape_html(a.context_field || "?")}`;
			else if (a.value_mode === "Expression") src = `<code>${frappe.utils.escape_html(a.expression || "?")}</code>`;
			else src = `"${frappe.utils.escape_html(a.value || "")}"`;
			return `set <b>${frappe.utils.escape_html(a.fieldname || "?")}</b> on ${frappe.utils.escape_html(a.target_doctype || "?")} to ${src}`;
		}
		case "Append Child Row":
			return `append a row to <b>${frappe.utils.escape_html(a.child_table || "?")}</b>`;
		case "Upsert Child Row":
			return `upsert a row in <b>${frappe.utils.escape_html(a.child_table || "?")}</b>`;
		case "Call Webhook":
			return `call webhook <b>${frappe.utils.escape_html(a.webhook_endpoint || "?")}</b>`;
		case "Create Note": {
			const txt = a.comment_mode === "Expression"
				? `<code>${frappe.utils.escape_html(a.comment_expression || "?")}</code>`
				: `"${frappe.utils.escape_html(a.comment_text || "")}"`;
			return `add a note ${txt} on the subject`;
		}
		case "Send WhatsApp":
			return `send WhatsApp template <b>${frappe.utils.escape_html(a.whatsapp_template || "?")}</b>`;
		case "Send Email":
			return `email <b>${frappe.utils.escape_html(a.email_recipient || "?")}</b>${a.email_subject ? `: "${frappe.utils.escape_html(a.email_subject)}"` : ""}`;
		case "Wait":
			return `wait <code>${frappe.utils.escape_html(a.wait_expression || "?")}</code>, then continue`;
		default:
			return "(choose an action)";
	}
}

function render_preview(frm) {
	if (!frm.doc) return;
	const trigger_phrase = frm.doc.on_doctype
		? `a <b>${frappe.utils.escape_html(frm.doc.on_doctype)}</b> record ${_EVENT_VERB[frm.doc.event] || "fires"}`
		: "(choose an On DocType and Event)";
	const criteria = (frm.doc.criteria || []).map(criterion_phrase).filter(Boolean);
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
