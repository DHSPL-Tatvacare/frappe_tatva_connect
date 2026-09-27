// Desk Client Script — CRM Lead Import, on frappe's Data Import pattern: one action per stage, realtime progress, one reload.

const TATVA_LI_API = 'tatva_connect.lead_import.api.';

frappe.ui.form.on('CRM Lead Import', {
  setup(frm) {
    frappe.realtime.on('bulk_job_progress', (data) => tatva_li_progress(frm, data));
    frappe.realtime.on('lead_import_refresh', ({ lead_import }) => {
      if (lead_import === frm.doc.name) frm.reload_doc();
    });
    $(frm.wrapper).on('dirty', () => frm.enable_save());
  },

  onload(frm) {
    frm.set_query('contract', () => ({ query: TATVA_LI_API + 'contract_query' }));
  },

  refresh(frm) {
    frm.set_intro(''); // frappe appends every banner, and a save refreshes twice
    frm.set_intro(tatva_li_next_step(frm), 'blue');
    tatva_li_section_options(frm);
    tatva_li_action(frm);
    tatva_li_progress_from_job(frm);
  },
});

frappe.ui.form.on('CRM Lead Import Column', {
  form_render(frm, cdt, cdn) {
    tatva_li_field_options(frm, cdn);
  },
  target_table(frm, cdt, cdn) {
    tatva_li_field_options(frm, cdn);
  },
});

// The one line that says where the operator is and what to do next.
function tatva_li_next_step(frm) {
  const d = frm.doc;
  if (frm.is_new()) return __('Pick the contract, then save.');
  if (!d.import_file) return __('Attach the CSV or XLSX file. Download Template gives the columns this contract accepts.');
  return {
    Draft: __('Check each column maps to the right field, or tick Skip. Then validate — nothing is written until you import.'),
    Validating: __('Validating every row. Nothing is saved to the database in this path.'),
    Validated: __('{0} rows passed, {1} refused. Choose the Bulk Lane, then import the rows that passed.', [d.valid_rows, d.invalid_rows]),
    'Validation Failed': __('No row passed. Open Import Results, fix the file and attach it again.'),
    Importing: __('Importing.'),
    Imported: __('{0} rows imported.', [d.valid_rows]),
    'Partially Imported': __('{0} rows imported, {1} refused. Open Import Results for the reasons.', [d.valid_rows, d.invalid_rows]),
    'Import Failed': __('The import stopped. Open the Import Job for the reason.'),
    Cancelled: __('The import was cancelled.'),
  }[d.status];
}

// One action per stage in place of Save, as the server names it; an edit brings Save back until it is saved.
function tatva_li_action(frm) {
  if (frm.is_new() || frm.is_dirty()) return;
  frm.disable_save(true);
  const d = frm.doc;
  const stage = (d.__onload || {}).next_stage;
  if (!d.import_file) {
    frm.add_custom_button(__('All Fields'), () => tatva_li_download(frm), __('Download Template'));
    frm.add_custom_button(__('Choose Fields'), () => tatva_li_choose_fields(frm), __('Download Template'));
  } else if (stage === 'validate') {
    frm.page.set_primary_action(__('Validate'), () => tatva_li_run(frm, 'start_validation'));
  } else if (stage === 'import') {
    frm.page.set_primary_action(__('Import'), () => {
      if (!d.bulk_lane) {
        frm.scroll_to_field('bulk_lane'); // frappe's own mandatory check: scroll to the field, then say so
        return frappe.msgprint({ title: __('Missing Fields'), message: __('{0} is required.', [__('Bulk Lane')]), indicator: 'red' });
      }
      frappe.confirm(__('{0} rows will be written in the {2} Bulk Lane. {1} refused rows will be skipped.', [d.valid_rows, d.invalid_rows, d.bulk_lane]),
        () => tatva_li_run(frm, 'start_import'));
    });
  } else if (stage === 'stop') {
    frm.page.set_primary_action(__('Stop'), () => frappe.confirm(tatva_li_stop_warning(d), () => tatva_li_run(frm, 'stop_import')));
  } else if (d.import_job || d.dry_run_job) {
    frm.page.set_primary_action(__('View Results'), () =>
      frappe.set_route('List', 'CRM Bulk Job Result', { job: d.import_job || d.dry_run_job }));
  }
}

// A Live run has already started journeys and sent what it sent, so the warning says what a stop cannot take back.
function tatva_li_stop_warning(d) {
  const base = __('Stop this run? The patients it has already created are removed, and an update onto an existing patient stays.');
  if (d.status === 'Validating' || d.bulk_lane !== 'Live') return base;
  return base + ' ' + __('Journeys and tasks raised for those patients end with them, but a message already sent cannot be taken back.');
}

// A blank file of every field the contract takes, or of `keys`; the server adds the lead's identity either way.
function tatva_li_download(frm, keys) {
  open_url_post('/api/method/' + TATVA_LI_API + 'download_template',
    { lead_import: frm.doc.name, ...(keys && { keys: JSON.stringify(keys) }) });
}

// The contract's fields in the app's one pick-rows dialog; the ticked ones become the template.
function tatva_li_choose_fields(frm) {
  frappe.call(TATVA_LI_API + 'template_fields', { lead_import: frm.doc.name }).then(({ message: fields = [] }) => {
    const identity = fields.filter((f) => f.identity).map((f) => f.label).join(', ');
    tatva_pick_rows({
      title: __('Choose Fields'),
      note: identity ? __('Tick the fields your file needs. {0} is always in it.', [identity])
        : __('Tick the fields your file needs.'),
      action_label: __('Download'),
      columns: [
        { fieldname: 'section', label: __('Section'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 3 },
        { fieldname: 'label', label: __('Field'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 7 },
      ],
      rows: fields,
      on_pick: (picked) => tatva_li_download(frm, picked.map((f) => f.field_key)),
    });
  });
}

function tatva_li_run(frm, method) {
  frappe.call({ method: TATVA_LI_API + method, args: { lead_import: frm.doc.name }, freeze: true })
    .then(() => frm.reload_doc());
}

// The worker announces each committed chunk; draw it only for this import's own running job.
function tatva_li_progress(frm, { job, processed, total }) {
  const d = frm.doc;
  if (!['Validating', 'Importing'].includes(d.status)) return;
  if (job !== (d.status === 'Validating' ? d.dry_run_job : d.import_job)) return;
  const title = d.status === 'Validating' ? __('Validating') : __('Importing');
  frm.dashboard.show_progress(title, (processed * 100) / total, __('{0} of {1} rows', [processed, total]));
}

// A reload redraws the bar from the running job's own row; the event above is only the fast path.
function tatva_li_progress_from_job(frm) {
  const d = frm.doc;
  const job = { Validating: d.dry_run_job, Importing: d.import_job }[d.status];
  if (!job) return;
  frappe.db.get_value('CRM Bulk Job', job, ['processed', 'total']).then(({ message: m }) => {
    if (m && m.total) tatva_li_progress(frm, { job, processed: m.processed, total: m.total });
  });
}

function tatva_li_section_options(frm) {
  if (frm.is_new()) return;
  const grid = frm.fields_dict.columns && frm.fields_dict.columns.grid;
  frappe.call({
    method: TATVA_LI_API + 'list_sections',
    args: { lead_import: frm.doc.name },
    callback(r) {
      tatva_set_grid_column_options(grid, 'target_table', (r && r.message) || []);
    },
  });
}

function tatva_li_field_options(frm, cdn) {
  const row = (frm.doc.columns || []).find((c) => c.name === cdn);
  if (!row || !row.target_table) return;
  const grid = frm.fields_dict.columns && frm.fields_dict.columns.grid;
  frappe.call({
    method: TATVA_LI_API + 'list_fields',
    args: { lead_import: frm.doc.name, section: row.target_table },
    callback(r) {
      const data = (r && r.message) || [];
      tatva_set_grid_row_options(grid, cdn, 'target_field', data);
      if (!data.length) {
        frappe.show_alert({ message: __('This contract writes no field in that section.'), indicator: 'orange' });
      }
    },
  });
}
