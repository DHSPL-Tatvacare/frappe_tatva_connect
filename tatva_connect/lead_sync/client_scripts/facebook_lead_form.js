// Desk Client Script — Facebook Lead Form: the questions grid's `mapped_to_crm_field` dropdown is fed from the ONE lead brain, scoped to the grain of this form's contract (server-side, has_permission-gated); native Autocomplete set_data, no DOM injection.

frappe.ui.form.on('Facebook Lead Form', {
  refresh(frm) {
    tatva_fb_cache_mappable_fields(frm);
    if (!frm.is_new()) frm.add_custom_button(__('Check against Meta'), () => tatva_fb_check_against_meta(frm));
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

// Meta's last week of leads for this form against the CRM, in one native dialog; the only action re-syncs what went missing.
function tatva_fb_check_against_meta(frm) {
  frappe.call({
    method: 'tatva_connect.lead_sync.api.check_against_meta',
    args: { facebook_lead_form: frm.doc.name },
    freeze: true,
    freeze_message: __('Asking Meta…'),
  }).then(({ message: r }) => {
    const stat = (label, value) => `<div class="col"><div class="text-muted small">${label}</div><div class="h4">${value}</div></div>`;
    const summary = `<div class="row">${stat(__('Meta'), r.meta)}${stat(__('In CRM'), r.in_crm)}${stat(__('Failed'), r.failed)}${stat(__('Missing'), r.missing)}</div>`
      + (r.truncated ? `<p class="text-muted small">${__('Meta has more leads than one check reads; the counts cover the newest.')}</p>` : '')
      + (r.rows.length ? '' : `<p>${__('Every lead Meta received in the last {0} days is in the CRM.', [r.days])}</p>`);
    const d = new frappe.ui.Dialog({
      title: __('Check against Meta · last {0} days', [r.days]),
      size: 'large',
      fields: [
        { fieldtype: 'HTML', fieldname: 'summary', options: summary },
        {
          fieldtype: 'Table', fieldname: 'rows', read_only: 1, cannot_add_rows: 1, cannot_delete_rows: 1,
          hidden: !r.rows.length, data: r.rows,
          fields: [
            { fieldtype: 'Data', fieldname: 'lead_id', label: __('Lead (Meta id)'), in_list_view: 1, read_only: 1 },
            { fieldtype: 'Datetime', fieldname: 'received', label: __('Received'), in_list_view: 1, read_only: 1 },
            { fieldtype: 'Data', fieldname: 'state', label: __('State'), in_list_view: 1, read_only: 1 },
            { fieldtype: 'Link', fieldname: 'log', label: __('Log'), options: 'Failed Lead Sync Log', in_list_view: 1, read_only: 1 },
            { fieldtype: 'Data', fieldname: 'why', label: __('Why'), in_list_view: 1, read_only: 1 },
          ],
        },
      ],
      primary_action_label: r.missing ? __('Re-sync missing ({0})', [r.missing]) : null,
      primary_action: r.missing ? () => tatva_fb_resync_missing(frm, d) : null,
    });
    d.show();
  });
}

function tatva_fb_resync_missing(frm, d) {
  frappe.call({
    method: 'tatva_connect.lead_sync.api.resync_missing',
    args: { facebook_lead_form: frm.doc.name },
    freeze: true,
    freeze_message: __('Re-syncing from Meta…'),
  }).then(({ message: r }) => {
    d.hide();
    frappe.show_alert({ message: __('{0} re-synced, {1} failed and logged.', [r.synced, r.failed]), indicator: r.failed ? 'orange' : 'green' });
  });
}
