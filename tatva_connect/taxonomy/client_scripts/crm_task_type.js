// Desk Client Script — CRM Task Type (Frappe Desk, /app/crm-task-type).
// Everything shown is decided server-side; this file paints and never judges.
//   1) Rules grid -> each row's When Field / Value / Add Target dropdowns, fed by tatva_set_grid_row_options.
//   2) Add Target -> appends the picked fieldname to Targets and clears itself (D26).
//   3) Schema grid -> a Lead row's Fieldname; and every row's Target, offered from the home it declares.
//   4) Enforcement -> the location condition's Field and Value, from this type's own declared questions.
//   5) Depends On goes read-only on a row a Show/Hide rule names, because the compile replaces it there.
// EVERY option set comes from the TYPE'S OWN declaration, read off frm.doc — the open form, saved or not —
// so a field added a moment ago is offered immediately. The one exception is a Target aimed at a section,
// whose columns belong to that section's doctype and are read through frappe's own meta cache.
// Static teaching text is NOT here — it is `description` in the DocType JSON (§17.5).
// The grid mechanism lives in tatva_connect.bundle.js (tatva_set_grid_*), shared by every mapping surface;
// a top-level field is painted with core's own set_df_property, which needs no bundle at all.
// With this script dead the page is fully usable: every painted control is a plain typed field.

frappe.ui.form.on('CRM Task Type', {
  refresh(frm) {
    tatva_task_location_options(frm);
  },
  // The condition's field changed — the Value list belongs to the newly chosen field.
  location_condition_field(frm) {
    tatva_task_location_options(frm);
  },
});

frappe.ui.form.on('CRM Task Type Field', {
  // A schema row was expanded — a Lead-sourced row's Fieldname offers the pickable lead fields (D31),
  // its Target offers the columns of the home it declares, and Depends On tells the truth about the rules.
  form_render(frm, cdt, cdn) {
    tatva_task_field_row_options(frm, cdt, cdn);
    tatva_task_target_options(frm, cdt, cdn);
    tatva_task_depends_on_state(frm, cdt, cdn);
  },
  // Source flipped — the Fieldname list belongs to the newly chosen source.
  source(frm, cdt, cdn) {
    tatva_task_field_row_options(frm, cdt, cdn);
  },
  // Section flipped — Target's candidates are that section's columns, so the list is re-asked.
  section(frm, cdt, cdn) {
    tatva_task_target_options(frm, cdt, cdn);
  },
  // A new question can be named in a rule, and a renamed one changes what the location condition may ask.
  fieldname(frm) {
    tatva_task_location_options(frm);
  },
});

frappe.ui.form.on('CRM Task Type Rule', {
  // A row was expanded — populate ITS dropdowns from the type's own schema.
  form_render(frm, cdt, cdn) {
    tatva_task_rule_row_options(frm, cdt, cdn);
  },
  // The When Field changed — the Value list belongs to the newly chosen field.
  condition_field(frm, cdt, cdn) {
    tatva_task_rule_row_options(frm, cdt, cdn);
  },
  // A field was picked to append: add it to Targets, then clear the helper so it can be used again.
  add_target(frm, cdt, cdn) {
    tatva_task_rule_append_target(frm, cdt, cdn);
  },
});

// ---- the type's own schema, as dropdown data --------------------------------

// {value,label} pairs for every QUESTION the open type asks. Frappe escapes these in the dropdown, and
// `add_options` reads .label/.value, so the admin reads the label while the fieldname is what is stored.
// A layout row stores nothing, so it holds no answer to test and is excluded: the server refuses one as a
// When Field ("is a layout row and holds no value to test") and `_declared_questions` keeps one out of a
// location condition, so offering one would offer a pick the save rejects. `frappe.model.no_value_type` is
// the client twin of the server's `NO_VALUE_FIELDS` — the same ten entries, so no third list is kept here.
function tatva_task_questions(frm) {
  return (frm.doc.schema || [])
    .filter((f) => (f.fieldname || '').trim() && !frappe.model.no_value_type.includes(f.fieldtype))
    .map((f) => ({ value: f.fieldname, label: f.label || f.fieldname }));
}

// Every declared row, layout markers INCLUDED — a rule may legitimately target a Section Break, which is
// how a whole section is shown or hidden. Only the condition side excludes them.
function tatva_task_rule_fields(frm) {
  return (frm.doc.schema || [])
    .filter((f) => (f.fieldname || '').trim())
    .map((f) => ({ value: f.fieldname, label: f.label || f.fieldname }));
}

// The options the chosen When Field declares, one per line (Select) — free text when it declares none.
function tatva_task_rule_values(frm, fieldname) {
  const field = (frm.doc.schema || []).find((f) => f.fieldname === fieldname);
  return ((field && field.options) || '')
    .split('\n')
    .map((o) => o.trim())
    .filter(Boolean)
    .map((o) => ({ value: o, label: o }));
}

// Per-row, because the Value list depends on THAT row's When Field; the controls exist only while the row
// is open, which is why this hangs off form_render.
function tatva_task_rule_row_options(frm, cdt, cdn) {
  const row = locals[cdt] && locals[cdt][cdn];
  const grid = frm.fields_dict.rules && frm.fields_dict.rules.grid;
  if (!row || !grid) return;
  tatva_set_grid_row_options(grid, cdn, 'condition_field', tatva_task_questions(frm));
  tatva_set_grid_row_options(grid, cdn, 'add_target', tatva_task_rule_fields(frm));
  tatva_set_grid_row_options(grid, cdn, 'condition_value', tatva_task_rule_values(frm, row.condition_field));
}

// ---- the location condition (Enforcement) -----------------------------------

// Top-level Selects, so core's own set_df_property paints them and no bundle helper is involved. The field
// list is this type's own questions — the SAME source a rule's When Field uses, because the location gate is
// evaluated by the same seam (`location.api._condition_holds` -> `_rule_atom` -> `_field_visible`) and the
// save refuses a condition naming anything else (`_validate_location_condition`).
function tatva_task_location_options(frm) {
  frm.set_df_property('location_condition_field', 'options',
    [{ value: '', label: '' }, ...tatva_task_questions(frm)]);
  frm.set_df_property('location_condition_value', 'options',
    [{ value: '', label: '' }, ...tatva_task_rule_values(frm, frm.doc.location_condition_field)]);
}

// ---- Target: the columns the router will actually honour ---------------------

// Per row, because the candidates belong to THAT row's Section. Asked of the server, which reads
// `field_target`'s own inputs — the section's target doctype, or CRM Task Field's settable columns — so the
// list offered is exactly the set the router honours. Cached per section for the life of the form: a schema
// grid is opened row by row and re-asking per row would be one call per click.
function tatva_task_target_options(frm, cdt, cdn) {
  const row = locals[cdt] && locals[cdt][cdn];
  const grid = frm.fields_dict.schema && frm.fields_dict.schema.grid;
  if (!row || !grid) return;
  if (frappe.model.no_value_type.includes(row.fieldtype)) {
    tatva_set_grid_row_options(grid, cdn, 'target', []);  // a marker stores nothing, so it targets nothing
    return;
  }
  tatva_task_target_columns(frm, row.section || '').then((cols) =>
    tatva_set_grid_row_options(grid, cdn, 'target', cols));
}

function tatva_task_target_columns(frm, section) {
  frm._tatva_target_cols = frm._tatva_target_cols || {};
  if (frm._tatva_target_cols[section]) return frm._tatva_target_cols[section];
  frm._tatva_target_cols[section] = frappe
    .call('tatva_connect.taxonomy.doctype.crm_task_type.crm_task_type.list_target_columns', { section })
    .then((r) => (r.message || []).map((c) => ({ value: c.fieldname, label: c.label || c.fieldname })));
  return frm._tatva_target_cols[section];
}

// ---- Depends On: read-only where the rules decide it ------------------------

// The fieldnames a comma-separated `targets` declaration names — the client twin of the server's
// `activity.api.rule_targets`, and the ONE reading of it here. Two copies had already drifted: one kept
// empty entries and the other filtered them.
function tatva_task_targets(text) {
  return (text || '').split(',').map((t) => t.trim()).filter(Boolean);
}

// `_compiled_visibility` returns the hand-typed condition ONLY when no Show and no Hide rule names the
// field; the moment one does, the compiled expression replaces it. So on such a row the box would display a
// value that will not run. Its twin `mandatory_depends_on` is read-only outright for the same reason — this
// one stays writable where it is still the author's, and goes read-only where it is not.
function tatva_task_depends_on_state(frm, cdt, cdn) {
  const row = locals[cdt] && locals[cdt][cdn];
  const grid = frm.fields_dict.schema && frm.fields_dict.schema.grid;
  if (!row || !grid) return;
  const ruled = (frm.doc.rules || []).some(
    (r) => ['Show', 'Hide'].includes(r.action) &&
      tatva_task_targets(r.targets).includes(row.fieldname));
  const field = grid.grid_rows_by_docname[cdn] &&
    grid.grid_rows_by_docname[cdn].grid_form &&
    grid.grid_rows_by_docname[cdn].grid_form.fields_dict.depends_on;
  if (!field) return;
  field.df.read_only = ruled ? 1 : 0;
  field.df.description = ruled
    ? __('A Show or Hide rule names this field, so the rules decide when it appears and anything typed here is replaced.')
    : __("Optional condition under which this field is shown, e.g. eval:doc.outcome=='Connected'. Any Rule naming this field replaces what is typed here, so a rule-driven field is left blank.");
  field.refresh();
}

// ---- the lead-field picker (D31) --------------------------------------------

// The server's own gate decides the list (`list_lead_fields` reads the lead-field catalogue at this grain);
// this only paints it, and only on a Lead-sourced row — an Activity row's fieldname is a NEW key the admin
// is naming. It is deliberately NOT the write allowlist: a lead field here is snapshotted, never written.
function tatva_task_field_row_options(frm, cdt, cdn) {
  const row = locals[cdt] && locals[cdt][cdn];
  const grid = frm.fields_dict.schema && frm.fields_dict.schema.grid;
  if (!row || !grid) return;
  if ((row.source || 'Activity') !== 'Lead') {
    tatva_set_grid_row_options(grid, cdn, 'fieldname', []);
    return;
  }
  tatva_lead_field_options(frm).then((fields) => tatva_set_grid_row_options(grid, cdn, 'fieldname', fields));
}

// One fetch per open form, re-asked only when the grain changes; the axes come off frm.doc, saved or not,
// so the list narrows the moment an operator picks a grain — same shape as the intake builder's fetch.
function tatva_lead_field_options(frm) {
  const axes = [frm.doc.vertical || '', frm.doc.group || '', frm.doc.program || ''].join('::');
  if (frm._tatva_lead_axes === axes && frm._tatva_lead_fields) return frm._tatva_lead_fields;
  frm._tatva_lead_axes = axes;
  frm._tatva_lead_fields = frappe
    .call('tatva_connect.taxonomy.doctype.crm_task_type.crm_task_type.list_lead_fields', {
      vertical: frm.doc.vertical,
      group: frm.doc.group,
      program: frm.doc.program,
    })
    .then((r) => (r.message || []).map((f) => ({ value: f.fieldname, label: f.label || f.fieldname })));
  return frm._tatva_lead_fields;
}

// ---- the append helper ------------------------------------------------------

// Targets is comma-separated text (a child table cannot hold a multiselect), so appending is a text edit:
// the pick is added once, never twice, and the helper empties itself so the next pick reads clean.
function tatva_task_rule_append_target(frm, cdt, cdn) {
  const row = locals[cdt] && locals[cdt][cdn];
  const picked = row && (row.add_target || '').trim();
  if (!picked) return;
  const existing = tatva_task_targets(row.targets);
  if (!existing.includes(picked)) existing.push(picked);
  frappe.model.set_value(cdt, cdn, 'targets', existing.join(', '));
  frappe.model.set_value(cdt, cdn, 'add_target', '');
}
