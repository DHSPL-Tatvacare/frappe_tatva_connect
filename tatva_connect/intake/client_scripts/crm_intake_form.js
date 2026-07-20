// Desk Client Script — CRM Intake Form builder (Frappe Desk, /app/crm-intake-form).
// Two affordances, both native, no DOM hacks, no innerHTML of user content:
//   1) Mappings grid dropdowns driven by LIVE meta:
//        - target_table  -> live CRM Lead Section keys + `note`, column-wide; the section brain is the only source.
//        - target_field  -> the row's pickable fields, resolved server-side by list_target_fields, per-row.
//        - show_if_field -> the OTHER rows' source_field values, column-wide.
//   2) Buttons: Publish/Unpublish (gated server call) and Advanced (native Web Form builder).
// 100% no-op until the form has a linked Web Form (i.e. has been saved & scaffolded).
// The grid mechanism lives in tatva_connect.bundle.js (tatva_set_grid_*), shared by every mapping surface.

frappe.ui.form.on('CRM Intake Form', {
  refresh(frm) {
    tatva_intake_buttons(frm);
    tatva_intake_showif_options(frm);
    tatva_intake_target_table_options(frm);
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
  tatva_set_grid_column_options(grid, 'show_if_field', Array.from(new Set(names)));
}

// target_table: the live CRM Lead Section keys + note, column-wide (same for all rows). No
// hardcoded list — reads the section brain, so it can never drift from what the fold routes on.
function tatva_intake_target_table_options(frm) {
  const grid = frm.fields_dict.mappings && frm.fields_dict.mappings.grid;
  if (!grid) return;
  frappe.db.get_list('CRM Lead Section', { fields: ['section_key'], order_by: 'display_order asc' }).then((rows) => {
    tatva_set_grid_column_options(grid, 'target_table', [...(rows || []).map((r) => r.section_key), 'note']);
  });
}

// target_field: per-row, from the ONE brain (CRM Lead API Field) scoped to the row's target_table AND
// the form's grain. The grain is read off frm.doc — the OPEN form, saved or not — so choosing/changing
// a grain re-narrows the list on the next dropdown open, with no save and no reject-after-the-fact.
function tatva_intake_target_field_options(frm, cdt, cdn) {
  const row = locals[cdt] && locals[cdt][cdn];
  if (!row || !(row.target_table || '').trim()) return;

  frappe.call({
    method: 'tatva_connect.intake.api.list_target_fields',
    args: {
      target_table: row.target_table,
      intake_form: frm.doc.name,
      vertical: frm.doc.custom_vertical,
      group: frm.doc.custom_group,
      program: frm.doc.custom_current_program,
    },
    callback(r) {
      const fields = (r && r.message) || [];
      // {value,label} pairs — frappe escapes these in the dropdown; we never build HTML.
      const data = fields.map((f) => ({ value: f.fieldname, label: f.label || f.fieldname }));
      const grid = frm.fields_dict.mappings && frm.fields_dict.mappings.grid;
      tatva_set_grid_row_options(grid, cdn, 'target_field', data);
      if (!data.length && row.target_table !== 'note') {
        // Empty list has exactly one cause worth naming: no grain chosen yet.
        const g = [frm.doc.custom_vertical, frm.doc.custom_group, frm.doc.custom_current_program];
        if (!g.some((x) => (x || '').trim())) {
          frappe.show_alert({ message: __('Pick the Vertical / Group / Program first — the field list is grain-scoped.'), indicator: 'orange' });
        }
      }
    },
  });
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
