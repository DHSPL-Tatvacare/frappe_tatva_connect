// Desk Client Script — CRM Lead Import, folded into frappe 16.52's Data Import wizard: its steps, screens and components over our import.

const TATVA_LI_API = 'tatva_connect.lead_import.api.';

frappe.ui.form.on('CRM Lead Import', {
  setup(frm) {
    frappe.realtime.on('bulk_job_progress', (data) => tatva_li_progress(frm, data));
    frappe.realtime.on('lead_import_refresh', ({ lead_import }) => {
      if (lead_import === frm.doc.name) frm.reload_doc();
    });
    $(frm.wrapper).on('dirty', () => frm._li_wizard && tatva_li_footer(frm));
  },

  onload(frm) {
    frm.set_query('contract', () => ({ query: TATVA_LI_API + 'contract_query' }));
  },

  refresh(frm) {
    tatva_li_section_options(frm);
    tatva_li_preview(frm);
    tatva_li_wizard(frm);
    tatva_li_progress_from_job(frm);
    tatva_history_button(frm);
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

// What to do first, said when the operator reaches for a step the import has not reached.
function tatva_li_next_step(frm) {
  const d = frm.doc;
  if (frm.is_new() || !(d.__onload || {}).has_source) return __('Pick the contract and attach the CSV or XLSX file, or give a Google Sheets URL.');
  return {
    Draft: __('Check the column mapping, then validate. Nothing is written until you import.'),
    Validating: __('Validating every row. Nothing is written in this run.'),
    'Validation Failed': __('No row passed. Read the reasons under Fix issues, fix the file and attach it again.'),
    Validated: __('Start the import from Fix issues; the Import step opens once it runs.'),
  }[d.status];
}

// The one action this stage offers, as the server names it; the wizard's footer draws it.
function tatva_li_stage(frm) {
  const d = frm.doc;
  const stage = (d.__onload || {}).next_stage;
  if (stage === 'validate') return { stage, run: () => tatva_li_run(frm, 'start_validation') };
  if (stage === 'import') return { stage, run: () => tatva_li_confirm_import(frm) };
  if (stage === 'stop') return { stage, run: () => frappe.confirm(tatva_li_stop_warning(d), () => tatva_li_run(frm, 'stop_import')) };
  return null;
}

function tatva_li_confirm_import(frm) {
  const d = frm.doc;
  if (!d.bulk_lane) {
    tatva_li_go(frm, 0);
    frm.scroll_to_field('bulk_lane'); // frappe's own mandatory check: scroll to the field, then say so
    return frappe.msgprint({ title: __('Missing Fields'), message: __('{0} is required.', [__('Bulk Lane')]), indicator: 'red' });
  }
  // The Live lane's ticked notification choices, named by the fields' own labels.
  const quiet = d.bulk_lane === 'Live' && d.skip_notifications ? [__(frm.fields_dict.skip_notifications.df.label)] : [];
  const msg = __('{0} rows will be written in the {2} Bulk Lane. {1} refused rows will be skipped.', [d.valid_rows, d.invalid_rows, d.bulk_lane]);
  frappe.confirm(quiet.length ? msg + ' ' + __('Ticked: {0}.', [quiet.join(', ')]) : msg, () => tatva_li_run(frm, 'start_import'));
}

// A Live run has already started journeys and sent what it sent, so the warning says what a stop cannot take back.
function tatva_li_stop_warning(d) {
  if (d.status === 'Validating') return __('Stop validating? Nothing has been written.');
  const base = __('Stop this run? The patients it has already created are removed, and an update onto an existing patient stays.');
  if (d.bulk_lane !== 'Live') return base;
  return base + ' ' + __('Journeys and tasks raised for those patients end with them, but a message already sent cannot be taken back.');
}

// A blank file of the chosen field keys; the server adds the lead's identity.
function tatva_li_download(frm, keys) {
  open_url_post('/api/method/' + TATVA_LI_API + 'download_template', { lead_import: frm.doc.name, keys: JSON.stringify(keys) });
}

// Download Template: the contract's fields in the app's one pick-rows dialog (its header box ticks them all); the ticked ones become the file.
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

// The source's first rows under the saved mapping, in the grid frappe's own import preview draws; shown until the import runs.
function tatva_li_preview(frm) {
  const $field = frm.get_field('preview').$wrapper.empty();
  if (!['validate', 'import'].includes((frm.doc.__onload || {}).next_stage)) return;
  frappe.call(TATVA_LI_API + 'preview', { lead_import: frm.doc.name }).then(({ message: p }) => {
    const esc = frappe.utils.escape_html;
    const heading = (c) => {
      const [colour, field] = c.skip ? ['gray', __('Skipped')] : c.field ? ['green', c.field] : ['orange', __('Not mapped')];
      return `<span class="indicator ${colour}">${esc(c.header)} → ${esc(field)}</span>`;
    };
    $field.empty().append($('<p class="text-muted small">').text(__('First {0} of {1} rows, under the mapping as saved.', [p.rows.length, p.row_count])));
    new frappe.DataTable($('<div>').appendTo($field).get(0), {
      columns: p.columns.map((c) => ({ name: esc(c.header), content: heading(c), editable: false, align: 'left', width: 200 })),
      data: p.rows.map((row) => row.map((cell) => esc(cell == null ? '' : String(cell)))),
      layout: p.columns.length < 10 ? 'fluid' : 'fixed',
      cellHeight: 35,
      serialNoColumn: false,
      checkboxColumn: false,
      noDataMessage: __('No Data'),
      disableReorderColumn: true,
    });
  });
}

function tatva_li_run(frm, method) {
  frappe.call({ method: TATVA_LI_API + method, args: { lead_import: frm.doc.name }, freeze: true })
    .then(() => frm.reload_doc());
}

// The job this import runs now, or none: its dry run while Validating, its live run while Importing.
function tatva_li_running_job(d) {
  return (d.__onload || {}).running_job;  // the server's one answer (api.running_job)
}

function tatva_li_section_options(frm) {
  if (frm.is_new()) return;
  const grid = frm.fields_dict.columns && frm.fields_dict.columns.grid;
  frappe.call({ method: TATVA_LI_API + 'list_sections', args: { lead_import: frm.doc.name } })
    .then((r) => tatva_set_grid_column_options(grid, 'target_table', (r && r.message) || []));
}

function tatva_li_field_options(frm, cdn) {
  const row = (frm.doc.columns || []).find((c) => c.name === cdn);
  if (!row || !row.target_table) return;
  const grid = frm.fields_dict.columns && frm.fields_dict.columns.grid;
  frappe.call({ method: TATVA_LI_API + 'list_fields', args: { lead_import: frm.doc.name, section: row.target_table } })
    .then((r) => {
      const data = (r && r.message) || [];
      tatva_set_grid_row_options(grid, cdn, 'target_field', data);
      if (!data.length) {
        frappe.show_alert({ message: __('This contract writes no field in that section.'), indicator: 'orange' });
      }
    });
}

// The worker announces each committed chunk; draw it only for this import's own running job, in the step's progress slot.
function tatva_li_progress(frm, { job, processed, total }) {
  if (!frm._li_wizard || job !== tatva_li_running_job(frm.doc)) return;
  frm._li_wizard.show_progress(frm.doc.status === 'Validating' ? __('Validating') : __('Importing'), processed, total, __('rows'));
}

// A reload redraws the bar from the running job's own row; the event above is only the fast path.
function tatva_li_progress_from_job(frm) {
  const job = tatva_li_running_job(frm.doc);
  if (!job) return;
  frappe.db.get_value('CRM Bulk Job', job, ['processed', 'total']).then(({ message: m }) => {
    if (m) tatva_li_progress(frm, { job, processed: m.processed || 0, total: m.total || 0 });
  });
}

// Data Import's four steps; the form's own fields are moved into each step's panel, the Data Import way.
const TATVA_LI_STEPS = [__('Config'), __('Preview'), __('Fix issues'), __('Import')];
const TATVA_LI_LAST = TATVA_LI_STEPS.length - 1;
const TATVA_LI_RUN = ['Importing', 'Imported', 'Partially Imported', 'Import Failed', 'Cancelled']; // a live run's statuses
const TATVA_LI_LOG_LIMIT = 1000; // frappe's IMPORT_LOG_PREVIEW_LIMIT: past it the log shows Export Import Log
const TATVA_LI_FIELDS = ['contract', 'program', 'source', 'google_sheets_url', 'columns', 'preview', 'bulk_lane', 'skip_notifications'];

// The furthest step this import's state has reached, read off what the server already returns.
function tatva_li_reached(frm) {
  const d = frm.doc;
  if (frm.is_new() || !(d.__onload || {}).has_source) return 0;
  return TATVA_LI_RUN.includes(d.status) ? TATVA_LI_LAST : 2; // Import opens once a live run has started, as frappe's
}

// The step the form opens on: a fresh file on Preview to check its mapping, a checked one on Fix issues, a run on Import.
function tatva_li_landing(frm) {
  const reached = tatva_li_reached(frm);
  return reached === 2 && frm.doc.status === 'Draft' ? 1 : reached;
}

// Built once per form; the shell lands, follows and stays by this import's own reached and landing steps.
function tatva_li_wizard(frm) {
  const w = frm._li_wizard || (frm._li_wizard = tatva_li_wizard_build(frm));
  if (!w) return;
  w.sync(tatva_li_reached(frm), tatva_li_landing(frm));
  tatva_li_render(frm);
}

// The shared Data Import shell over this import's own steps; the last is done once the run imported.
function tatva_li_wizard_build(frm) {
  return tatva_wizard_shell(frm, {
    steps: TATVA_LI_STEPS,
    label: __('Import steps'),
    fields: TATVA_LI_FIELDS,
    finished: () => ['Imported', 'Partially Imported'].includes(frm.doc.status),
    on_step_click: (i) => tatva_li_go(frm, i),
  });
}

// Redraw the current step through the shell, then the footer.
function tatva_li_render(frm) {
  const w = frm._li_wizard;
  w.render(w.step, ($c) => [tatva_li_config, tatva_li_mapping, tatva_li_fix_issues, tatva_li_import][w.step](frm, $c));
  tatva_li_footer(frm);
}

// Config, as frappe's: three sections in reading order, each two columns, each conditional field beside the choice that brings it.
function tatva_li_config(frm, $c) {
  const w = frm._li_wizard;
  $c.append($('<div class="diw-config-step flex flex-col gap-4">').append(
    $('<div class="diw-config-section">').append(w.head(__('Import settings')), w.columns(['contract', 'program'], ['source'])),
    $('<div class="diw-config-section mt-4 pt-4 border-t">').append(w.head(__('Lane and notifications')), w.columns(['bulk_lane'], ['skip_notifications'])),
    tatva_li_upload(frm)));
}

// Upload file: Download Template, then the file or sheet card once one is set, else Frappe's File upload / Google Sheet tabs.
function tatva_li_upload(frm) {
  const d = frm.doc;
  const template = !frm.is_new() && d.contract
    ? frappe.ui.button({ label: __('Download Template'), variant: 'outline', onclick: () => tatva_li_choose_fields(frm) }) : null;
  const $upload = $('<div class="diw-upload-section mt-4 pt-4 border-t">').append(frm._li_wizard.head(__('Upload file'), template));
  if (frm.is_new()) return $upload.append($('<div class="text-sm text-ink-gray-6 py-6">').text(__('Pick the contract, then press Next to upload a file or Google Sheet.')));
  if (d.import_file) return $upload.append(tatva_li_file_card(frm));
  if (d.google_sheets_url && !frm.is_dirty()) return $upload.append(tatva_li_sheet_card(frm));
  return $upload.append(frappe.ui.tabs({
    active: d.google_sheets_url ? 1 : 0,
    css_class: 'diw-upload-tabs',
    tabs: [
      { label: __('File upload'), icon: 'upload', content: tatva_li_dropzone(frm) },
      { label: __('Google Sheet'), icon: 'link', content: frm._li_wizard.place($('<div class="diw-frm-field w-full pt-4">'), ['google_sheets_url']) },
    ],
  }));
}

// Data Import's drop zone over the Attach field's own uploader: a click opens it, a drop hands it the file.
function tatva_li_dropzone(frm) {
  const control = frm.fields_dict.import_file;
  const open = (files) => {
    control.set_upload_options();
    new frappe.ui.FileUploader({ ...control.upload_options, ...(files ? { files } : {}) });
  };
  const max_mb = Math.round((frappe.boot.max_file_size || 0) / (1024 * 1024));
  const $zone = $(`
    <div class="diw-frm-field diw-file-dropzone-host w-full pt-4">
      <div class="diw-file-dropzone-target">
        <div class="diw-file-dropzone-ui flex flex-col items-center text-center gap-1 pointer-events-none">
          <div class="diw-file-dropzone-icon text-ink-gray-6">${frappe.utils.icon('cloud-upload', 'md', '', '', '', true)}</div>
          <div class="text-sm text-ink-gray-6 max-w-sm">${__('Drag a CSV or Excel file here, or click to browse')}</div>
          ${max_mb ? `<div class="text-xs text-ink-gray-5">${__('.csv, .xlsx up to {0} MB', [max_mb])}</div>` : ''}
        </div>
      </div>
    </div>`);
  const $target = $zone.find('.diw-file-dropzone-target');
  $target.on('click', () => open());
  $target.on('dragover', (e) => { e.preventDefault(); $zone.addClass('is-dragover'); });
  $target.on('dragleave', () => $zone.removeClass('is-dragover'));
  $target.on('drop', (e) => {
    e.preventDefault();
    e.stopPropagation(); // else the form's own file drop opens a second uploader as a plain attachment
    $zone.removeClass('is-dragover');
    const files = e.originalEvent.dataTransfer.files;
    if (files.length) open(files);
  });
  return $zone;
}

// The file is fixed while a run reads it and for good once a live run has written from it, as Data Import's (the server holds the same line).
function tatva_li_source_locked(d) {
  return Boolean(tatva_li_running_job(d) || d.import_job);
}

// Data Import's file card: the file's own name from its File row, its type and rows, and Clear through the Attach field.
function tatva_li_file_card(frm) {
  const url = frm.doc.import_file;
  const $name = $('<a class="diw-import-file-card-name block truncate text-base-semibold text-ink-gray-9" target="_blank" rel="noopener noreferrer">').attr('href', url);
  const $meta = $('<div class="text-sm text-ink-gray-6">');
  const show = (name) => {
    $name.text(name).attr('title', name);
    $meta.text([(name.split('.').pop() || '').toUpperCase(), __('{0} rows', [frm.doc.row_count || 0])].filter(Boolean).join(' · '));
  };
  show(decodeURIComponent(url.split('/').pop().split('?')[0]));
  frappe.db.get_value('File', { file_url: url }, 'file_name').then(({ message: m }) => m && m.file_name && show(m.file_name));
  return $('<div class="diw-import-file-card flex items-center justify-between gap-3 w-full border rounded-lg mt-4 px-4 py-3 text-sm bg-surface-base">').append(
    $('<div class="flex items-center gap-2 min-w-0 flex-1">').append(
      $('<div class="diw-card-icon-well flex items-center justify-center shrink-0 size-10 rounded bg-surface-gray-2 text-ink-gray-7">').html(frappe.utils.icon('file-spreadsheet', 'md', '', '', '', true)),
      $('<div class="min-w-0">').append($name, $meta)),
    tatva_li_source_locked(frm.doc) ? [] : frappe.ui.button({ label: __('Clear'), variant: 'outline', onclick: () => frm.fields_dict.import_file.clear_attachment() }));
}

// Data Import's sheet card: the saved URL and a Clear that saves it away.
function tatva_li_sheet_card(frm) {
  const url = frm.doc.google_sheets_url;
  return $('<div class="diw-google-sheet-card flex items-center justify-between gap-2 w-full rounded mt-4 p-2 text-sm bg-surface-gray-2">').append(
    $('<div class="flex items-center gap-2 min-w-0 flex-1">').append(
      $('<span class="inline-flex shrink-0 text-ink-gray-6">').html(frappe.utils.icon('link', 'sm', '', '', '', true)),
      $('<a class="diw-google-sheet-card-url min-w-0 truncate text-ink-base" target="_blank" rel="noopener noreferrer">').attr('href', url).attr('title', url).text(url)),
    tatva_li_source_locked(frm.doc) ? [] : frappe.ui.button({ label: __('Clear'), size: 'xs', variant: 'outline', onclick: () => frm.set_value('google_sheets_url', '').then(() => frm.save()) }));
}

// Preview: the column mapping grid and the first rows under it.
function tatva_li_mapping(frm, $c) {
  frm._li_wizard.place($('<div class="diw-preview-step flex flex-col gap-4">'), ['columns', 'preview']).appendTo($c);
}

// Fix issues, as frappe's: the check runs here, its issues show here, and the import starts from here.
function tatva_li_fix_issues(frm, $c) {
  const d = frm.doc;
  if (d.status === 'Validating') return frm._li_wizard.progress($c);
  const matched = (d.columns || []).filter((c) => c.target_field && !c.skip).length;
  if (!d.dry_run_job || d.status === 'Draft') {
    return $c.append(frappe.ui.empty_state({
      icon: 'list-checks',
      title: __('Check every row before importing'),
      description: __('Validate runs each row through the real import and rolls it back. Nothing is written until you import.'),
      css_class: 'min-h-96',
    }).append(frm._li_wizard.stats([[d.row_count, __('Rows in file')], [matched, __('Columns matched')], [(d.columns || []).length - matched, __('Columns skipped')]])));
  }
  const refused = cint(d.invalid_rows);
  const checked = cint(d.valid_rows) + refused;
  const done = TATVA_LI_RUN.includes(d.status); // the run's counts now; its own log is on Import
  if (!refused || done) {
    return $c.append(frappe.ui.empty_state({
      icon: 'list-checks',
      title: __('No issues to fix'),
      description: done ? __('This import is complete. There are no pending warnings or mapping issues.')
        : __('No warnings or mapping issues were found. You can continue to import.'),
      css_class: 'min-h-96',
    }).append(frm._li_wizard.stats([[checked, __('Rows checked')], [matched, __('Columns matched')], [refused, __('Rows skipped')]])));
  }
  const download = frappe.ui.button({ label: __('Download Failed Rows'), variant: 'outline', size: 'sm', icon_left: 'download',
    onclick: () => open_url_post('/api/method/' + TATVA_LI_API + 'download_refused_rows', { lead_import: d.name, job: d.dry_run_job }) });
  const hint = d.status === 'Validation Failed' ? __('No row passed. Fix the file and attach it again, or go Back to change the mapping, then Retry.')
    : __('{0} rows passed and will be written; the {1} failed rows are skipped when you import. Go Back to change the mapping, then Retry, to fix them first.', [d.valid_rows, refused]);
  $c.append(frm._li_wizard.head(__('Rows that failed validation'), download),
    $('<p class="text-sm text-ink-gray-6 mb-2">').text(hint),
    $(frm._li_wizard.stats([[checked, __('Rows checked')], [matched, __('Columns matched')], [refused, __('Rows failed'), 'text-ink-red-6']])).addClass('mb-4'));
  frappe.db.get_list('CRM Bulk Job Result', { filters: { job: d.dry_run_job, action: 'failed' },
    fields: ['record_index', 'action', 'record_name', 'error_message'], order_by: 'record_index asc', limit: TATVA_LI_LOG_LIMIT })
    .then((rows) => $c.append(tatva_li_rows_table(frm, rows)));
}

// Result rows as Data Import's Row · Status · Message table.
function tatva_li_rows_table(frm, rows) {
  const word = { created: __('Created'), merged: __('Updated'), failed: __('Failure') };
  return frm._li_wizard.table([[__('Row'), '10%'], [__('Status'), '14%'], [__('Message'), '76%']], rows.map((r) => {
    const failed = r.action === 'failed';
    const message = failed ? frappe.utils.escape_html(r.error_message || '')
      : r.record_name ? `<a href="${frappe.utils.get_form_link('CRM Lead', r.record_name)}">${frappe.utils.escape_html(r.record_name)}</a>` : '';
    return [cint(r.record_index) + 2, frappe.ui.badge.html({ label: word[r.action] || r.action, theme: failed ? 'red' : 'green' }), message];
  }), __('No rows to show'));
}

// Import: the run's progress, then its Import Log.
function tatva_li_import(frm, $c) {
  if (frm.doc.status === 'Importing') return frm._li_wizard.progress($c);
  tatva_li_log(frm, $c, frm.doc.import_job);
}

// Data Import's Import Log for one run: Result and Details, then its rows filtered All / Success / Failed.
function tatva_li_log(frm, $c, job, filter = 'all') {
  const results = (action) => ({ job, ...(action ? { action } : {}) });
  $c.data('li-filter', filter); // as frappe's render_import_log: the old log stays until the new one is fetched, then swaps
  Promise.all([
    frappe.db.get_value('CRM Bulk Job', job, ['total', 'succeeded', 'failed', 'partner', 'submitted_at', 'finished_at']),
    frappe.db.count('CRM Bulk Job Result', { filters: results('created') }),
    frappe.db.count('CRM Bulk Job Result', { filters: results('merged') }),
    frappe.db.get_list('CRM Bulk Job Result', {
      filters: filter === 'all' ? results() : { job, action: [filter === 'failed' ? '=' : '!=', 'failed'] },
      fields: ['record_index', 'action', 'record_name', 'error_message'], order_by: 'record_index asc', limit: TATVA_LI_LOG_LIMIT,
    }),
  ]).then(([{ message: j }, created, merged, rows]) => {
    if (!j || $c.data('li-filter') !== filter) return; // a later tab click won
    const kv = (label, value, action) => `<tr class="diw-log-kv-row">
        <td class="diw-log-kv-key text-sm text-ink-gray-6"><div class="diw-log-kv-key-inner flex items-center gap-2"><span>${frappe.utils.escape_html(label)}</span>${action || ''}</div></td>
        <td class="diw-log-kv-val text-sm text-ink-gray-8 text-end"><div class="diw-log-kv-val-inner flex items-center justify-end">${value}</div></td></tr>`;
    const num = (v, extra = '') => `<span class="text-sm-semibold ${extra}">${cint(v)}</span>`;
    const download = j.failed ? frappe.ui.button.html({ variant: 'ghost', size: 'xs', icon: 'download', attrs: { 'data-action': 'download_refused', title: __('Download Failed Rows') } }) : '';
    // frappe's Go to list, once a live run is done: here the leads this run wrote.
    const done = ['Imported', 'Partially Imported'].includes(frm.doc.status);
    const to_list = done ? frappe.ui.button.html({ label: __('Go to list'), variant: 'ghost', size: 'xs', icon_left: 'external-link', attrs: { 'data-action': 'go_to_list' } }) : '';
    const result = [kv(__('Total rows'), num(j.total), to_list), kv(__('Created'), num(created)), kv(__('Updated'), num(merged)),
      kv(__('Failed'), num(j.failed, 'text-ink-red-6'), download)];
    const when = (v) => (v ? frappe.datetime.str_to_user(v) : '-');
    const details = [kv(__('Started by'), frappe.utils.escape_html(frappe.user.full_name(j.partner))),
      kv(__('Started on'), when(j.submitted_at)), kv(__('Finished on'), when(j.finished_at))];
    const $log = $(`
      <div>
        ${frm._li_wizard.head(__('Import Log')).prop('outerHTML')}
        <div class="diw-import-log-summary mb-4"><div class="diw-import-log-summary-grid flex gap-8">
          <div class="diw-log-kv-col flex-1 min-w-0"><div class="diw-log-kv-caption text-xs text-ink-gray-6 mb-1">${__('Result')}</div><table class="diw-log-kv-table w-full">${result.join('')}</table></div>
          <div class="diw-log-kv-col flex-1 min-w-0"><div class="diw-log-kv-caption text-xs text-ink-gray-6 mb-1">${__('Details')}</div><table class="diw-log-kv-table w-full">${details.join('')}</table></div>
        </div></div>
        <div class="diw-import-log-filter-row mt-3 mb-3 flex items-center justify-between gap-2"><div class="diw-import-log-filter-tabs"></div><div class="diw-import-log-filter-actions inline-flex items-center ms-auto"></div></div>
        ${tatva_li_rows_table(frm, rows)}
      </div>`);
    const passed = cint(j.total) - cint(j.failed);
    $log.find('.diw-import-log-filter-tabs').append(new frappe.ui.TabButtons({
      label: __('Filter import log'),
      value: filter,
      options: [
        { label: __('All ({0})', [cint(j.total)]), value: 'all' },
        { label: __('Success ({0})', [passed]), value: 'success' },
        { label: __('Failed ({0})', [cint(j.failed)]), value: 'failed' },
      ],
      on_change: (value) => tatva_li_log(frm, $c, job, value),
    }).el);
    frappe.utils.bind_actions_with_object($log, {
      download_refused: () => open_url_post('/api/method/' + TATVA_LI_API + 'download_refused_rows', { lead_import: frm.doc.name, job }),
      go_to_list: () => frappe.db.get_list('CRM Bulk Job Result', { filters: { job, action: ['!=', 'failed'] }, fields: ['record_name'], limit: 0 }).then((r) => {
        frappe.route_options = { name: ['in', r.map((x) => x.record_name)] };
        frappe.set_route('List', 'CRM Lead');
      }),
    });
    if (cint(j.total) > TATVA_LI_LOG_LIMIT) {
      $log.find('.diw-import-log-filter-actions').append(frappe.ui.button({ label: __('Export Import Log'), variant: 'outline', size: 'sm', icon_left: 'download',
        onclick: () => open_url_post('/api/method/' + TATVA_LI_API + 'download_import_log', { lead_import: frm.doc.name, job }) }));
    }
    $c.empty().append($log);
  });
}

// The footer through the shell; on the step reached, the server's stage is the action: Validate, then Retry after a check or a run, Import, Stop.
function tatva_li_footer(frm) {
  const w = frm._li_wizard;
  const stage = w.step === w.reached && tatva_li_stage(frm);
  const again = stage && stage.stage === 'validate' && frm.doc.status !== 'Draft';
  const action = stage && { label: again ? __('Retry') : { validate: __('Validate'), import: __('Import'), stop: __('Stop') }[stage.stage], icon: again ? 'refresh-cw' : '', run: stage.run };
  w.footer(action, (i) => tatva_li_go(frm, i));
}

// Move to step `i` through the shell, or say what to do first.
function tatva_li_go(frm, i) {
  if (!frm._li_wizard.go(i, tatva_li_next_step(frm))) return;
  tatva_li_render(frm);
  tatva_li_progress_from_job(frm);
}
