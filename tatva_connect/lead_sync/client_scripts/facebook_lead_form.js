// Desk Client Script — Facebook Lead Form: the questions grid's `mapped_to_crm_field` dropdown is fed from the ONE lead brain, scoped to the grain of this form's contract (server-side, has_permission-gated); native Autocomplete set_data, no DOM injection.

frappe.ui.form.on('Facebook Lead Form', {
  refresh(frm) {
    tatva_fb_cache_mappable_fields(frm);
    if (!frm.is_new()) frm.add_custom_button(__('Validate from Meta'), () => tatva_fb_ask_window(frm));
  },

  onload_post_render(frm) {
    if (frm.__tatva_fb_bound) return;
    frm.__tatva_fb_bound = true;
    frappe.realtime.on('fb_check_ready', (report) => {
      if (report.form !== frm.doc.name) return;
      tatva_fb_done(frm);
      tatva_fb_show_report(frm, report);
    });
    frappe.realtime.on('fb_check_failed', ({ form }) => {
      if (form !== frm.doc.name) return;
      tatva_fb_done(frm);
      frappe.msgprint({
        title: __('The validation could not finish'),
        message: __('Meta could not be read. The reason is in the Error Log.'),
        indicator: 'red',
      });
    });
    // The re-sync runs on a worker; the same progress bar the lead import draws follows it.
    frappe.realtime.on('fb_resync_progress', ({ form, processed, total }) => {
      if (form !== frm.doc.name) return;
      frm.dashboard.show_progress(__('Re-syncing'), (processed * 100) / total,
        __('{0} of {1} leads', [processed, total]));
    });
    frappe.realtime.on('fb_resync_done', ({ form, linked, failed }) => {
      if (form !== frm.doc.name) return;
      frm.dashboard.hide_progress();
      frappe.msgprint({
        title: __('Re-sync finished'),
        message: __('{0} now Linked in CRM. {1} Failed. Validate from Meta again to see the counts.', [linked, failed]),
        indicator: failed ? 'orange' : 'green',
      });
    });
  },
});

// One freeze owns the whole wait, and never for ever: a lost socket must not strand the screen.
function tatva_fb_wait(frm, queued) {
  if (!queued) return tatva_fb_done(frm);
  frm.__tatva_fb_timer = setTimeout(() => {
    tatva_fb_done(frm);
    frappe.msgprint({
      title: __('Still running'),
      message: __('The validation is taking longer than usual. Run it again to see the result.'),
      indicator: 'orange',
    });
  }, 180000);
}

function tatva_fb_done(frm) {
  clearTimeout(frm.__tatva_fb_timer);
  frappe.dom.unfreeze();
}

// How far back to ask Meta. A wider window is more Graph pages, so it is the operator's choice and not a default they never saw.
function tatva_fb_ask_window(frm) {
  const d = new frappe.ui.Dialog({
    title: __('Validate from Meta'),
    fields: [{
      fieldname: 'window', fieldtype: 'Select', label: __('How far back'), reqd: 1, default: '7',
      options: [
        { value: '7', label: __('Last 7 days') },
        { value: '14', label: __('Last 14 days') },
        { value: '30', label: __('Last 30 days') },
        { value: 'all', label: __('Everything Meta still holds') },
      ],
      description: __('At most 5,000 leads are read, newest first.'),
    }],
    primary_action_label: __('Validate'),
    primary_action: ({ window }) => {
      d.hide();
      frappe.dom.freeze(__('Asking Meta…'));
      frappe.call({
        method: 'tatva_connect.lead_sync.api.check_against_meta',
        args: { facebook_lead_form: frm.doc.name, window },
      }).then((r) => tatva_fb_wait(frm, r && r.message && r.message.queued))
        .catch(() => tatva_fb_done(frm));
    },
  });
  d.show();
}

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

// The finished report in one native dialog; the only action re-syncs everything left unlinked.
function tatva_fb_show_report(frm, r) {
  const unlinked = r.in_crm + r.failed + r.missing;
  const stat = (label, value) => `<div class="col"><div class="text-muted small">${label}</div><div class="h4">${value}</div></div>`;
  const summary = `<div class="row">${stat(__('Leads at Meta'), r.meta)}${stat(__('Linked in CRM'), r.linked)}${stat(__('Already in CRM, not linked'), r.in_crm)}${stat(__('Failed earlier'), r.failed)}${stat(__('Missing'), r.missing)}</div>`
    + `<p class="text-muted small">${__('Missing means no lead carries this id and none answers to this person. The rest are already in the CRM and only need this submission stamped onto them.')}</p>`
    + (r.truncated ? `<p class="text-muted small">${__('Meta has more leads than one run reads; the counts cover the newest.')}</p>` : '')
    + (r.listed > r.rows.length ? `<p class="text-muted small">${__('Showing the {0} most recent of {1} that need attention.', [r.rows.length, r.listed])}</p>` : '')
    + (r.rows.length ? '' : `<p>${__('Every lead Meta received {0} is linked in the CRM.', [tatva_fb_window_label(r.days)])}</p>`);
  const d = new frappe.ui.Dialog({
    title: __('Validate from Meta · {0}', [tatva_fb_window_label(r.days)]),
    size: 'large',
    fields: [
      { fieldtype: 'HTML', fieldname: 'summary', options: summary },
      {
        // Three columns, because frappe's grid splits the width evenly and a fourth truncates the timestamp.
        fieldtype: 'Table', fieldname: 'rows', read_only: 1, cannot_add_rows: 1, cannot_delete_rows: 1,
        hidden: !r.rows.length, data: r.rows.map((row) => ({ ...row, what: tatva_fb_state_label(row) })),
        fields: [
          { fieldtype: 'Datetime', fieldname: 'received', label: __('Received'), in_list_view: 1, read_only: 1 },
          { fieldtype: 'Data', fieldname: 'lead_id', label: __('Lead (Meta id)'), in_list_view: 1, read_only: 1 },
          { fieldtype: 'Data', fieldname: 'what', label: __('What happened'), in_list_view: 1, read_only: 1 },
        ],
      },
    ],
    // One button, one meaning: every unlinked lead the run found, folded on a worker.
    primary_action_label: unlinked ? __('Re-sync {0}', [unlinked]) : null,
    primary_action: unlinked ? () => tatva_fb_resync(frm, d) : null,
  });
  d.show();
}

// The row's state in the operator's words; a failed row names its log, which is where the reason is.
function tatva_fb_state_label(row) {
  if (row.state === 'failed') return __('Failed earlier: {0}', [row.log || row.why]);
  if (row.state === 'in_crm') return __('Already in CRM, not linked');
  return __('Missing: {0}', [row.why]);
}

// The window in words, so the title and the all-clear line read the same and neither has to know about null.
function tatva_fb_window_label(days) {
  return days ? __('last {0} days', [days]) : __('everything Meta still holds');
}

// Queue it and let go: the worker announces each lead, and `fb_resync_done` reports the outcome.
function tatva_fb_resync(frm, d) {
  frappe.call({
    method: 'tatva_connect.lead_sync.api.resync_missing',
    args: { facebook_lead_form: frm.doc.name },
  }).then(({ message: r }) => {
    d.hide();
    frm.dashboard.show_progress(__('Re-syncing'), 0, __('{0} of {1} leads', [0, (r && r.total) || 0]));
  });
}
