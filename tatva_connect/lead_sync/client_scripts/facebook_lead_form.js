// Desk Client Script — Facebook Lead Form: the questions grid's `mapped_to_crm_field` dropdown is fed from the ONE lead brain, scoped to the grain of this form's contract (server-side, has_permission-gated); native Autocomplete set_data, no DOM injection.

frappe.ui.form.on('Facebook Lead Form', {
  refresh(frm) {
    tatva_fb_cache_mappable_fields(frm);
  },
});

frappe.ui.form.on('Facebook Lead Form Question', {
  // A row was expanded — populate ITS dropdown from the form's cached field list.
  form_render(frm, cdt, cdn) {
    tatva_fb_set_mapped_field_options(frm, cdn);
  },
});

// Fetch once per form load; the set depends on the form's source + contract, never on the row.
function tatva_fb_cache_mappable_fields(frm) {
  if (!frm.doc.name || frm.doc.__islocal) return;
  frappe.call({
    method: 'tatva_connect.lead_sync.api.list_mappable_fields',
    args: { facebook_lead_form: frm.doc.name },
    callback(r) {
      frm.__tatva_mappable = (r && r.message) || [];
      if (!frm.__tatva_mappable.length) {
        // Empty list has one cause worth naming: no Lead Sync Source with a contract points here yet.
        frappe.show_alert({
          message: __('No contract yet — create a Lead Sync Source for this form and pick its Contract; the field list is grain-scoped.'),
          indicator: 'orange',
        });
      }
    },
  });
}

// Feed the opened grid row's Autocomplete through the shared helper (native control, no DOM injection).
function tatva_fb_set_mapped_field_options(frm, cdn) {
  const grid = frm.fields_dict.questions && frm.fields_dict.questions.grid;
  tatva_set_grid_row_options(grid, cdn, 'mapped_to_crm_field', frm.__tatva_mappable || []);
}
