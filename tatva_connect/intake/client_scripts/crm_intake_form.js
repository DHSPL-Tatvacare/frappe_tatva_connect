// Desk Client Script — CRM Intake Form builder (Frappe Desk, /app/crm-intake-form).
// Two affordances, both native, no DOM hacks, no innerHTML of user content:
//   1) Mappings grid dropdowns driven by LIVE meta:
//        - target_field  -> the pickable fields of the row's target_table (resolved server-side
//          via list_target_fields, which is has_permission-gated and read-only). Per-row, so it
//          is set on the OPENED row control in `form_render`.
//        - show_if_field -> the OTHER rows' source_field values (a function of the whole grid,
//          so it is set column-wide via grid.update_docfield_property).
//   2) Buttons: Publish/Unpublish (toggles the linked Web Form's `published` via a gated server
//      call) and Advanced (opens the native Web Form builder at /app/web-form/<route>).
// 100% no-op until the form has a linked Web Form (i.e. has been saved & scaffolded).
//
// Native grid mechanism (verified against frappe v15 grid.js / grid_row.js):
//   * update_docfield_property(fieldname, "options", [...]) feeds a Select/Autocomplete dropdown
//     for ALL rows — correct for show_if_field (same option set every row).
//   * For per-row options (target_field), the opened row's control is reached through
//     grid.grid_rows_by_docname[cdn].grid_form.fields_dict[fieldname] and fed via set_data /
//     "options" + refresh — escaped native controls only, never raw HTML.

frappe.ui.form.on('CRM Intake Form', {
  refresh(frm) {
    tatva_intake_buttons(frm);
    tatva_intake_showif_options(frm);
  },
});

frappe.ui.form.on('CRM Intake Field Map', {
  // A row was expanded — populate ITS target_field dropdown from the row's target_table.
  form_render(frm, cdt, cdn) {
    tatva_intake_target_field_options(frm, cdt, cdn);
  },
  // target_table changed on a row — refetch that row's target_field options.
  target_table(frm, cdt, cdn) {
    tatva_intake_target_field_options(frm, cdt, cdn);
  },
  // any source_field edit changes the show_if_field option set for every row.
  source_field(frm) {
    tatva_intake_showif_options(frm);
  },
  mappings_remove(frm) {
    tatva_intake_showif_options(frm);
  },
});

// ---- grid dropdowns ---------------------------------------------------------

// show_if_field: the OTHER rows' source_field values, column-wide (same for all rows).
function tatva_intake_showif_options(frm) {
  const grid = frm.fields_dict.mappings && frm.fields_dict.mappings.grid;
  if (!grid) return;
  const names = (frm.doc.mappings || [])
    .map((r) => (r.source_field || '').trim())
    .filter(Boolean);
  // Select control reads options as a "\n"-joined string; a leading blank allows clearing.
  const opts = ['', ...Array.from(new Set(names))].join('\n');
  try {
    grid.update_docfield_property('show_if_field', 'options', opts);
  } catch (e) {
    // Field absent / grid not built yet — safe no-op.
  }
}

// target_field: per-row, from live meta of the row's target_table.
function tatva_intake_target_field_options(frm, cdt, cdn) {
  const row = locals[cdt] && locals[cdt][cdn];
  if (!row || !(row.target_table || '').trim()) return;

  frappe.call({
    method: 'tatva_connect.intake.api.list_target_fields',
    args: { target_table: row.target_table, intake_form: frm.doc.name },
    callback(r) {
      const fields = (r && r.message) || [];
      // {value,label} pairs — frappe escapes these in the dropdown; we never build HTML.
      const data = fields.map((f) => ({ value: f.fieldname, label: f.label || f.fieldname }));
      tatva_intake_set_row_options(frm, cdn, 'target_field', data);
    },
  });
}

// Set a dropdown's options on a SINGLE opened grid row (native control, no DOM injection).
function tatva_intake_set_row_options(frm, cdn, fieldname, data) {
  const grid = frm.fields_dict.mappings && frm.fields_dict.mappings.grid;
  const grid_row = grid && grid.grid_rows_by_docname && grid.grid_rows_by_docname[cdn];
  const field = grid_row && grid_row.grid_form && grid_row.grid_form.fields_dict
    ? grid_row.grid_form.fields_dict[fieldname]
    : null;
  if (!field) return;
  // Autocomplete control: feed it the list; Select/Data fall back to a "\n" options string.
  if (typeof field.set_data === 'function') {
    field.set_data(data);
  } else {
    field.df.options = ['', ...data.map((d) => d.value)].join('\n');
    field.refresh();
  }
}

// ---- buttons ----------------------------------------------------------------

function tatva_intake_buttons(frm) {
  // No-op until the form is saved & scaffolded (the read-only doctype/route are stamped then).
  if (frm.is_new() || !frm.doc.web_form_doctype || !frm.doc.route) return;

  frappe.db.get_value('Web Form', { doc_type: frm.doc.web_form_doctype }, 'published').then((res) => {
    const published = res && res.message ? res.message.published : 0;
    const label = published ? __('Unpublish') : __('Publish');
    frm.add_custom_button(label, () => {
      frappe.call({
        method: 'tatva_connect.intake.api.toggle_published',
        args: { intake_form: frm.doc.name },
        callback(r) {
          const now = r && r.message ? __('Published') : __('Unpublished');
          frappe.show_alert({ message: now, indicator: r && r.message ? 'green' : 'orange' });
          frm.refresh();
        },
      });
    });
  });

  frm.add_custom_button(__('Advanced'), () => {
    window.open('/app/web-form/' + encodeURIComponent(frm.doc.route), '_blank');
  });

  // Submissions: open THIS form's own per-form submission doctype list. Generic — the route
  // is built from the form's stamped web_form_doctype, nothing enrolment-specific is hardcoded.
  // Gated on web_form_doctype (only a saved/scaffolded form has its sink), same as the buttons above.
  frm.add_custom_button(__('Submissions'), () => {
    frappe.set_route('List', frm.doc.web_form_doctype);
  });
}
