// Desk Client Script — CRM Task Type (Frappe Desk, /app/crm-task-type).
// Everything shown is decided server-side; this file paints and never judges.
//   1) Rules grid -> each row's When Field / Value / Add Target dropdowns, fed by tatva_set_grid_row_options.
//   2) Add Target -> appends the picked fieldname to Targets and clears itself (D26).
// The option sets come from the TYPE'S OWN schema, read off frm.doc — the open form, saved or not — so a
// field added a moment ago is offered immediately and nothing is fetched.
// Static teaching text is NOT here — it is `description` in the DocType JSON (§17.5).
// The grid mechanism lives in tatva_connect.bundle.js (tatva_set_grid_*), shared by every mapping surface.
// With this script dead the page is fully usable: every painted control is a plain typed field.

frappe.ui.form.on('CRM Task Type Field', {
  // A schema row was expanded — a Lead-sourced row's Fieldname offers the pickable lead fields (D31).
  form_render(frm, cdt, cdn) {
    tatva_task_field_row_options(frm, cdt, cdn);
  },
  // Source flipped — the Fieldname list belongs to the newly chosen source.
  source(frm, cdt, cdn) {
    tatva_task_field_row_options(frm, cdt, cdn);
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

// {value,label} pairs for every declared field of the open type. Frappe escapes these in the dropdown.
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
  const fields = tatva_task_rule_fields(frm);
  tatva_set_grid_row_options(grid, cdn, 'condition_field', fields);
  tatva_set_grid_row_options(grid, cdn, 'add_target', fields);
  tatva_set_grid_row_options(grid, cdn, 'condition_value', tatva_task_rule_values(frm, row.condition_field));
}

// ---- the lead-field picker (D31) --------------------------------------------

// The server's own gate decides the list (`list_lead_fields` filters by `is_settable`); this only paints
// it, and only on a Lead-sourced row — an Activity row's fieldname is a NEW key the admin is naming.
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
  const existing = (row.targets || '')
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean);
  if (!existing.includes(picked)) existing.push(picked);
  frappe.model.set_value(cdt, cdn, 'targets', existing.join(', '));
  frappe.model.set_value(cdt, cdn, 'add_target', '');
}
