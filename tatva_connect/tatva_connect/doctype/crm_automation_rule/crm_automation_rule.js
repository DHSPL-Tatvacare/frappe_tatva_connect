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
	refresh(frm) {
		reload_describe(frm);
		add_simulate_button(frm);
		apply_whatsapp_template_query(frm);
	},
	on_doctype: reload_describe,
	event: reload_describe,
	vertical: reload_describe,
	group: reload_describe,
	program: reload_describe,
});

frappe.ui.form.on("CRM Automation Criterion", {
	field(frm, cdt, cdn) {
		reset_invalid_operator(frm, cdt, cdn);
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
	sync_templates(frm, cdt, cdn) {
		frappe
			.call({
				method: "tatva_connect.whatsapp.templates_sync.sync_from_wati",
				freeze: true,
				freeze_message: __("Syncing WhatsApp templates"),
			})
			.then(() => {
				frappe.show_alert({ message: __("Templates synced"), indicator: "green" });
			})
			.catch(() => {
				frappe.show_alert({ message: __("Template sync failed, see the error above"), indicator: "red" });
			});
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
			apply_verb_options(frm);
			apply_action_query(frm);
			(frm.doc.actions || []).forEach((a) => apply_action_row(frm, a.doctype, a.name));
			render_preview(frm);
		});
}

function field_descriptor(frm, key) {
	return frm._schema ? frm._schema.fields.find((f) => f.key === key) || null : null;
}

// A frappe Select silently rewrites a model value its options can't render (controls/select.js
// set_formatted_input). So every stored value stays in its own row's option list even when the
// schema no longer offers it (a field dropped from the allowlist, a Select option retired) - merely
// OPENING a rule must never erase what it stores. validate() still rejects it on save.
function option_lines(offered, stored) {
	const kept = [...new Set(stored.filter((v) => v && !offered.includes(v)))];
	return [""].concat(offered, kept).join("\n");
}

// The criterion Field dropdown = builder_schema's `fields` (already read-allowlist-scoped server-side).
function apply_field_options(frm) {
	if (!frm.fields_dict.criteria) return;
	const offered = (frm._schema.fields || []).map((f) => f.key);
	const stored = (frm.doc.criteria || []).map((c) => c.field);
	frm.fields_dict.criteria.grid.update_docfield_property("field", "options", option_lines(offered, stored));
	frm.fields_dict.criteria.grid.refresh();
	(frm.doc.criteria || []).forEach((c) => apply_criterion_row(frm, c.doctype, c.name));
}

// The Action Type dropdown = builder_schema's `verbs` (actions._ACTION_LANES, the ONE verb registry).
// The doctype's own Select options are the pre-schema fallback only; sourcing the live list from the
// registry means a registered verb can never be unpickable, and a RETIRED verb still renders on an
// old rule (option_lines) instead of being silently blanked on open - validate() rejects it on save.
function apply_verb_options(frm) {
	if (!frm.fields_dict.actions) return;
	const offered = (frm._schema.verbs || []).map((v) => v.verb);
	const stored = (frm.doc.actions || []).map((a) => a.action_type);
	frm.fields_dict.actions.grid.update_docfield_property("action_type", "options", option_lines(offered, stored));
	frm.fields_dict.actions.grid.refresh();
}

const TRANSITION_OPERATORS = ["changed to", "changed from…to"];

// Valid Operators for a field's schema type, off builder_schema.operators_by_type - the one operator
// table, no hardcoded literal. The transition operators need a before-value, which only an Updated
// event carries.
function allowed_operators(frm, d) {
	if (!(d && frm._schema)) return [];
	const ops = frm._schema.operators_by_type[d.type] || [];
	return frm.doc.event === "Updated" ? ops : ops.filter((op) => !TRANSITION_OPERATORS.includes(op));
}

// Picking a new Field is a deliberate edit, so an operator its type can't take is dropped here - and
// ONLY here. Doing it on render would silently rewrite a stored rule the schema no longer describes.
function reset_invalid_operator(frm, cdt, cdn) {
	const c = locals[cdt][cdn];
	if (c.operator && !allowed_operators(frm, field_descriptor(frm, c.field)).includes(c.operator)) {
		frappe.model.set_value(cdt, cdn, "operator", "");
	}
}

// The per-row seam. `grid_row.docfields` is the row's OWN docfield copy (frappe.meta.get_docfields);
// its compact in-grid control is built from it LAZILY, on first edit - so a control-only seam
// (`set_field_property`) narrows nothing on a freshly rendered row. Write the row's docfield, then
// let `refresh_field` repaint the static cell and any live control. No manual DOM control (S.4).
function set_row_property(grid_row, fieldname, props) {
	const df = grid_row.docfields.find((d) => d.fieldname === fieldname);
	if (!df) return;
	Object.assign(df, props);
	grid_row.refresh_field(fieldname);
}

// Narrow ONE criterion row to its chosen field: its type's operators, and a typed Value/From Value
// control matching the field's `pick` (Link search / Select options / Date) instead of free text.
function apply_criterion_row(frm, cdt, cdn) {
	const c = locals[cdt] && locals[cdt][cdn];
	const grid_row = c && frm.fields_dict.criteria.grid.grid_rows_by_docname[cdn];
	if (!grid_row) return;
	const d = field_descriptor(frm, c.field);
	set_row_property(grid_row, "operator", { options: option_lines(allowed_operators(frm, d), [c.operator]) });
	apply_typed_control(grid_row, "value", d, c.value);
	apply_typed_control(grid_row, "from_value", d, c.from_value);
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

// Send WhatsApp's whatsapp_template picker is the server's account-aware link-query (each result
// described by its WATI account and the grains routing to it) instead of the doctype's own bare
// title search - see tatva_connect.whatsapp.templates.template_picker_query.
function apply_whatsapp_template_query(frm) {
	frm.set_query("whatsapp_template", "actions", () => ({
		query: "tatva_connect.whatsapp.templates.template_picker_query",
	}));
}

// Then side: Update Field's `fieldname` picks from builder_schema.set_targets (the grain's can_set
// allowlist) instead of free text. An action row is POLYMORPHIC - its params depend on its verb - and
// a grid has one column set for every row, so the grid lists the verb alone and every param is edited
// in the row form, where `depends_on` narrows per row. This runs for both (same docfield objects).
function apply_action_row(frm, cdt, cdn) {
	if (cdt !== "CRM Automation Action" || !frm._schema) return;
	const a = locals[cdt][cdn];
	if (a.action_type !== "Update Field") return;
	const grid_row = frm.fields_dict.actions.grid.grid_rows_by_docname[cdn];
	if (!grid_row) return;
	const targets = frm._schema.set_targets || [];
	set_row_property(grid_row, "fieldname", {
		fieldtype: targets.length ? "Select" : "Data",
		options: option_lines(targets.map((t) => t.key), [a.fieldname]),
	});
}

// Swap a row control's fieldtype/options to match a typed descriptor's `pick` - the one seam every
// typed value control (criterion Value/From Value) uses.
function apply_typed_control(grid_row, fieldname, d, current) {
	const [fieldtype, options] = control_shape(d, current);
	set_row_property(grid_row, fieldname, { fieldtype, options });
}

const TYPED_CONTROLS = ["Date", "Datetime", "Check", "Int", "Float", "Currency"];

// Key off the pick's own target/options rather than its `kind`: a child-table descriptor is
// kind="child" yet carries the inner field's Link target / Select options (describe.field_catalog),
// so a child criterion gets a real picker instead of a bare text box.
function control_shape(d, current) {
	if (!d) return ["Data", ""];
	const pick = d.pick || {};
	if (pick.target) return ["Link", pick.target];
	if (pick.options) return ["Select", option_lines(pick.options, [current])];
	if (TYPED_CONTROLS.includes(d.type)) return [d.type, ""];
	return ["Data", ""];
}

// -- live plain-English preview ---------------------------------------------

const _EVENT_VERB = { Created: "is created", Updated: "is updated", Deleted: "is deleted" };

// The preview renders onto frappe's `.form-message.blue` surface, which already picks its own text
// colour per theme (scss/desk/form.scss). Page-tuned tokens (--text-muted) and the global `code`
// styling both fight that surface, so nothing inside sets a colour: emphasis inherits, and "muted"
// is opacity. Theme-aware by construction, no hardcoded value (C.7).
function mono(text) {
	return `<code style="color:inherit;background:none;padding:0;">${text}</code>`;
}

function muted(text) {
	return `<span style="opacity:0.7;">${text}</span>`;
}

function grain_phrase(frm) {
	const parts = ["vertical", "group", "program"].map((a) => frm.doc[a]).filter(Boolean);
	return parts.length ? parts.join(" · ") : "no grain set";
}

function criterion_phrase(c) {
	if (!c.field) return null;
	const field = mono(frappe.utils.escape_html(c.field));
	const op = frappe.utils.escape_html(c.operator || "is");
	if (c.operator === "changed from…to") {
		return `${field} changed from ${mono(frappe.utils.escape_html(c.from_value || ""))} to ${mono(frappe.utils.escape_html(c.value || ""))}`;
	}
	if (c.operator === "is set" || c.operator === "is not set") {
		return `${field} ${op}`;
	}
	return `${field} ${op} ${mono(frappe.utils.escape_html(c.value || ""))}`;
}

function action_phrase(a) {
	switch (a.action_type) {
		case "Require Fields":
			return `require <b>${frappe.utils.escape_html(a.require_fields || "?")}</b> to be set (blocks save)`;
		case "Require Location":
			return `require a captured location${a.geofence_meters ? ` within <b>${a.geofence_meters}m</b>` : ""} (blocks save)`;
		case "Create Task": {
			const due = a.due_mode === "Expression"
				? ` due ${mono(frappe.utils.escape_html(a.due_expression || "?"))}`
				: (a.due_from ? ` due from ${mono(frappe.utils.escape_html(a.due_from))}` : "");
			return `create a <b>${frappe.utils.escape_html(a.task_type || "?")}</b> task${due}`;
		}
		case "Update Field": {
			let src;
			if (a.value_mode === "From Context") src = `context.${frappe.utils.escape_html(a.context_field || "?")}`;
			else if (a.value_mode === "Expression") src = mono(frappe.utils.escape_html(a.expression || "?"));
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
				? mono(frappe.utils.escape_html(a.comment_expression || "?"))
				: `"${frappe.utils.escape_html(a.comment_text || "")}"`;
			return `add a note ${txt} on the subject`;
		}
		case "Send WhatsApp":
			return `send WhatsApp template <b>${frappe.utils.escape_html(a.whatsapp_template || "?")}</b>`;
		case "Send Email":
			return `email <b>${frappe.utils.escape_html(a.email_recipient || "?")}</b>${a.email_subject ? `: "${frappe.utils.escape_html(a.email_subject)}"` : ""}`;
		case "Wait":
			return `wait ${mono(frappe.utils.escape_html(a.wait_expression || "?"))}, then continue`;
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
			${muted(`(${frappe.utils.escape_html(grain_phrase(frm))})`)}${when}
			${actions ? `<b> → then:</b><ul style="margin:4px 0 0 16px;">${actions}</ul>` : `<b> → then:</b> ${muted("no actions yet")}`}
		</div>`;
	frm.dashboard.clear_headline();
	frm.dashboard.set_headline(html);
}

// -- Simulate (Task 15) -------------------------------------------------------
//
// "Test before you enable": a Dialog that asks for one sample record of the rule's own On DocType
// (a native Link control, scoped by the dialog's own `options`, so the picker only offers records of
// the right type) and previews `automation.simulate.dry_run` — the SERVER is the tested core; this is
// UX only. Result rendering reuses the native HTML-field seam (`set_df_property("options", html)` +
// its own `refresh()`, the same mechanism `frm.dashboard.set_headline` already wraps above) into a
// panel this dialog owns - never `.$wrapper.html()` on someone else's component (S.4).

function add_simulate_button(frm) {
	if (frm.is_new() || !has_trigger(frm)) return;
	frm.add_custom_button("Simulate", () => open_simulate_dialog(frm));
}

function open_simulate_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: `Simulate — ${frappe.utils.escape_html(frm.doc.rule_name || frm.doc.name)}`,
		fields: [
			{
				fieldname: "sample_name",
				fieldtype: "Link",
				label: `Sample ${frm.doc.on_doctype}`,
				options: frm.doc.on_doctype,
				reqd: 1,
				description: "Pick a real record to preview this rule's criteria + actions against. Nothing is written.",
			},
			{ fieldname: "result_panel", fieldtype: "HTML" },
		],
		primary_action_label: "Run",
		primary_action: () => run_simulation(frm, dialog),
	});
	dialog.show();
}

function run_simulation(frm, dialog) {
	const sample_name = dialog.get_value("sample_name");
	if (!sample_name) return;
	set_simulate_panel(dialog, `<div style="color:var(--text-muted)">Running…</div>`);
	frappe
		.call({
			method: "tatva_connect.automation.simulate.dry_run",
			args: { rule_name: frm.doc.name, sample_doctype: frm.doc.on_doctype, sample_name },
		})
		.then((r) => render_simulate_result(dialog, r.message))
		.catch(() => {
			set_simulate_panel(dialog, `<div style="color:var(--text-danger)">Simulation failed — see the Error Log.</div>`);
		});
}

function set_simulate_panel(dialog, html) {
	dialog.set_df_property("result_panel", "options", html);
}

function simulate_action_list(rows) {
	if (!rows.length) return `<div style="color:var(--text-muted)">(none)</div>`;
	return `<ul style="margin:4px 0 0 16px;">${rows
		.map((r) => `<li><b>${frappe.utils.escape_html(r.verb)}</b> — ${frappe.utils.escape_html(r.would)}</li>`)
		.join("")}</ul>`;
}

function render_simulate_result(dialog, result) {
	const badge = result.matched
		? `<span style="color:var(--text-success)">MATCHED</span>`
		: `<span style="color:var(--text-muted)">not matched</span>`;
	const html = `
		<div style="padding:4px 0;">
			<div><b>Criteria:</b> ${badge}</div>
			<div style="margin-top:8px;"><b>Guards</b> ${simulate_action_list(result.guards)}</div>
			<div style="margin-top:8px;"><b>Effects</b> ${simulate_action_list(result.effects)}</div>
		</div>`;
	set_simulate_panel(dialog, html);
}
