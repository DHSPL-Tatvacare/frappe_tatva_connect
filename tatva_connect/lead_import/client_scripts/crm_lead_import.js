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
    frm.scroll_to_field('bulk_lane'); // frappe's own mandatory check: scroll to the field, then say so
    return frappe.msgprint({ title: __('Missing Fields'), message: __('{0} is required.', [__('Bulk Lane')]), indicator: 'red' });
  }
  // The Live lane's ticked notification choices, named by the fields' own labels.
  const quiet = d.bulk_lane === 'Live' ? ((d.__onload || {}).skip_fields || []).filter((f) => d[f]).map((f) => __(frm.fields_dict[f].df.label)) : [];
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
  const w = frm._li_wizard;
  if (!w || !w.$progress || job !== tatva_li_running_job(frm.doc)) return;
  const label = frm.doc.status === 'Validating' ? __('Validating') : __('Importing');
  w.$progress.empty().append(frappe.ui.progress({ label, size: 'md', value: total ? (processed * 100) / total : 0, hint: () => __('{0} of {1} rows', [processed, total]) }));
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
const TATVA_LI_FIELDS = ['contract', 'program', 'source', 'google_sheets_url', 'columns', 'preview', 'bulk_lane', 'skip_email', 'skip_in_app', 'skip_push'];

// The furthest step this import's state has reached, read off what the server already returns.
function tatva_li_reached(frm) {
  const d = frm.doc;
  if (frm.is_new() || !(d.__onload || {}).has_source) return 0;
  if (d.status === 'Draft') return 1;
  if (['Validating', 'Validation Failed'].includes(d.status)) return 2;
  return TATVA_LI_LAST;
}

// Built once per form; lands on the furthest step, follows the state forward, otherwise stays where the operator is.
function tatva_li_wizard(frm) {
  const reached = tatva_li_reached(frm);
  const w = frm._li_wizard || (frm._li_wizard = tatva_li_wizard_build(frm));
  if (w.docname !== frm.doc.name) w.step = null;
  w.docname = frm.doc.name;
  w.step = w.step == null || reached > w.reached ? reached : Math.min(w.step, reached);
  w.reached = reached;
  tatva_li_render(frm);
}

// Frappe's own Data Import shell: its page classes hide the std form, sidebar and Save; the steppers, the card, the footer.
function tatva_li_wizard_build(frm) {
  const w = { step: null, reached: 0, docname: null };
  const steps = TATVA_LI_STEPS.map((label) => ({ label }));
  w.stepper = new frappe.ui.Stepper({
    steps,
    label: __('Import steps'),
    is_locked: (i) => i > w.reached,
    is_completed: (i) => i < w.reached || (i === TATVA_LI_LAST && ['Imported', 'Partially Imported'].includes(frm.doc.status)),
    on_step_click: (i) => tatva_li_go(frm, i),
    on_locked_click: (i) => tatva_li_go(frm, i),
  });
  w.compact = new frappe.ui.Stepper({ steps, label: __('Import steps'), compact: true });
  frm.page.wrapper.addClass('data-import-custom-shell');
  frm.page.main.addClass('data-import-custom-page flex flex-col flex-1');
  frm.page.main.closest('.layout-main-section-wrapper').addClass('data-import-custom-wrapper');
  const $shell = $(`
    <div class="data-import-custom-ui flex justify-center w-full">
      <div class="diw-shell flex flex-col w-full min-w-0 gap-5">
        <div class="diw-stepper-wrap shrink-0 w-full"></div>
        <div class="diw-card flex flex-col w-full min-w-0 overflow-hidden rounded-lg shadow-sm border bg-surface-base">
          <div class="diw-mobile-step-header shrink-0 pt-4 px-4"></div>
          <div class="diw-step-content flex-1 min-h-0 min-w-0 w-full overflow-auto py-4 px-5"></div>
          <div class="diw-footer flex items-center justify-between shrink-0 w-full border-t mt-auto py-4 px-5">
            <div class="diw-footer-left"></div>
            <div class="diw-footer-right flex items-center gap-2"></div>
          </div>
        </div>
      </div>
    </div>`).prependTo(frm.page.main);
  $shell.find('.diw-stepper-wrap').append(w.stepper.$el);
  $shell.find('.diw-mobile-step-header').append(w.compact.$el);
  w.$content = $shell.find('.diw-step-content');
  w.$left = $shell.find('.diw-footer-left');
  w.$right = $shell.find('.diw-footer-right');
  return w;
}

// Redraw the steppers, the current step's panel and the footer.
function tatva_li_render(frm) {
  const w = frm._li_wizard;
  w.stepper.set_current(w.step);
  w.compact.set_current(w.step);
  TATVA_LI_FIELDS.forEach((f) => frm.fields_dict[f].$wrapper.detach()); // detach, not empty: the controls keep their handlers
  w.$content.empty();
  w.$progress = null;
  [tatva_li_config, tatva_li_mapping, tatva_li_fix_issues, tatva_li_import][w.step](frm, w.$content);
  frm.layout.refresh_dependency();
  tatva_li_footer(frm);
}

// Moves the form's own fields into a panel slot.
function tatva_li_place($into, frm, fieldnames) {
  fieldnames.forEach((f) => $into.append(frm.fields_dict[f].$wrapper));
  return $into;
}

// A Data Import section heading, with an optional action on its right.
function tatva_li_head(title, $action) {
  return $('<div class="flex items-center justify-between gap-4 mb-4">')
    .append($('<span class="diw-section-head-title text-base-semibold">').text(title), $action || []);
}

// Config: the contract and its source as Import settings, then Upload file.
function tatva_li_config(frm, $c) {
  const $grid = $('<div class="diw-config-grid grid items-start w-full gap-5">').append(
    tatva_li_place($('<div class="diw-config-column diw-config-main w-full min-w-0">'), frm, ['contract']),
    tatva_li_place($('<div class="diw-config-column diw-config-options w-full min-w-0">'), frm, ['source', 'program']));
  $c.append($('<div class="diw-config-step flex flex-col gap-4">').append(
    $('<div class="diw-config-section">').append(tatva_li_head(__('Import settings')), $grid),
    tatva_li_upload(frm)));
}

// Upload file: Download Template, then the file or sheet card once one is set, else Frappe's File upload / Google Sheet tabs.
function tatva_li_upload(frm) {
  const d = frm.doc;
  const template = !frm.is_new() && d.contract
    ? frappe.ui.button({ label: __('Download Template'), variant: 'outline', onclick: () => tatva_li_choose_fields(frm) }) : null;
  const $upload = $('<div class="diw-upload-section mt-4 pt-4 border-t">').append(tatva_li_head(__('Upload file'), template));
  if (frm.is_new()) return $upload.append($('<div class="text-sm text-ink-gray-6 py-6">').text(__('Pick the contract, then press Next to upload a file or Google Sheet.')));
  if (d.import_file) return $upload.append(tatva_li_file_card(frm));
  if (d.google_sheets_url && !frm.is_dirty()) return $upload.append(tatva_li_sheet_card(frm));
  return $upload.append(frappe.ui.tabs({
    active: d.google_sheets_url ? 1 : 0,
    css_class: 'diw-upload-tabs',
    tabs: [
      { label: __('File upload'), icon: 'upload', content: tatva_li_dropzone(frm) },
      { label: __('Google Sheet'), icon: 'link', content: tatva_li_place($('<div class="diw-frm-field w-full pt-4">'), frm, ['google_sheets_url']) },
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
    $zone.removeClass('is-dragover');
    const files = e.originalEvent.dataTransfer.files;
    if (files.length) open(files);
  });
  return $zone;
}

// Data Import's file card: the file's own name from its File row, its type and rows, and Clear through the Attach field.
function tatva_li_file_card(frm) {
  const url = frm.doc.import_file;
  const running = !!tatva_li_running_job(frm.doc);
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
    running ? [] : frappe.ui.button({ label: __('Clear'), variant: 'outline', onclick: () => frm.fields_dict.import_file.clear_attachment() }));
}

// Data Import's sheet card: the saved URL and a Clear that saves it away.
function tatva_li_sheet_card(frm) {
  const url = frm.doc.google_sheets_url;
  const running = !!tatva_li_running_job(frm.doc);
  return $('<div class="diw-google-sheet-card flex items-center justify-between gap-2 w-full rounded mt-4 p-2 text-sm bg-surface-gray-2">').append(
    $('<div class="flex items-center gap-2 min-w-0 flex-1">').append(
      $('<span class="inline-flex shrink-0 text-ink-gray-6">').html(frappe.utils.icon('link', 'sm', '', '', '', true)),
      $('<a class="diw-google-sheet-card-url min-w-0 truncate text-ink-base" target="_blank" rel="noopener noreferrer">').attr('href', url).attr('title', url).text(url)),
    running ? [] : frappe.ui.button({ label: __('Clear'), size: 'xs', variant: 'outline', onclick: () => frm.set_value('google_sheets_url', '').then(() => frm.save()) }));
}

// Preview: the column mapping grid and the first rows under it.
function tatva_li_mapping(frm, $c) {
  tatva_li_place($('<div class="diw-preview-step flex flex-col gap-4">'), frm, ['columns', 'preview']).appendTo($c);
}

// Fix issues: the dry run's progress while it runs, then its Import Log.
function tatva_li_fix_issues(frm, $c) {
  if (frm.doc.status === 'Validating') return tatva_li_running(frm, $c);
  if (!frm.doc.dry_run_job) return $c.append(frappe.ui.empty_state({ icon: 'inbox', title: __('Validate to see which rows pass.'), css_class: 'min-h-32' }));
  tatva_li_log(frm, $c, frm.doc.dry_run_job);
}

// Import: the lane and notification choices before a run, its progress during one, its Import Log after.
function tatva_li_import(frm, $c) {
  const d = frm.doc;
  if (d.status === 'Importing') return tatva_li_running(frm, $c);
  if (d.status !== 'Validated') return tatva_li_log(frm, $c, d.import_job);
  $c.append(tatva_li_head(__('Import settings')),
    $('<p class="text-sm text-ink-gray-6 mb-4">').text(__('{0} rows will be written. {1} refused rows will be skipped.', [d.valid_rows, d.invalid_rows])),
    tatva_li_place($('<div class="diw-config-grid grid items-start w-full gap-5">'), frm, ['bulk_lane', 'skip_email', 'skip_in_app', 'skip_push']));
}

// A run in flight: the progress slot the worker's events fill.
function tatva_li_running(frm, $c) {
  frm._li_wizard.$progress = $('<div class="diw-import-progress-standard py-6">').appendTo($c);
}

// Data Import's Import Log for one run: Result and Details, then its rows filtered All / Success / Failed.
function tatva_li_log(frm, $c, job, filter = 'all') {
  const dry = job === frm.doc.dry_run_job;
  const results = (action) => ({ job, ...(action ? { action } : {}) });
  Promise.all([
    frappe.db.get_value('CRM Bulk Job', job, ['total', 'succeeded', 'failed', 'partner', 'submitted_at', 'finished_at']),
    dry ? null : frappe.db.count('CRM Bulk Job Result', { filters: results('created') }),
    dry ? null : frappe.db.count('CRM Bulk Job Result', { filters: results('merged') }),
    frappe.db.get_list('CRM Bulk Job Result', {
      filters: filter === 'all' ? results() : { job, action: [filter === 'failed' ? '=' : '!=', 'failed'] },
      fields: ['record_index', 'action', 'record_name', 'error_message'], order_by: 'record_index asc', limit: 500,
    }),
  ]).then(([{ message: j }, created, merged, rows]) => {
    if (!j) return;
    const kv = (label, value, action) => `<tr class="diw-log-kv-row">
        <td class="diw-log-kv-key text-sm text-ink-gray-6"><div class="diw-log-kv-key-inner flex items-center gap-2"><span>${frappe.utils.escape_html(label)}</span>${action || ''}</div></td>
        <td class="diw-log-kv-val text-sm text-ink-gray-8 text-end"><div class="diw-log-kv-val-inner flex items-center justify-end">${value}</div></td></tr>`;
    const num = (v, extra = '') => `<span class="text-sm-semibold ${extra}">${cint(v)}</span>`;
    const download = j.failed ? frappe.ui.button.html({ variant: 'ghost', size: 'xs', icon: 'download', attrs: { 'data-action': 'download_refused', title: __('Download Failed Rows') } }) : '';
    const result = [kv(__('Total rows'), num(j.total))].concat(dry
      ? [kv(__('Passed'), num(j.succeeded))]
      : [kv(__('Created'), num(created)), kv(__('Updated'), num(merged))], [kv(__('Failed'), num(j.failed, 'text-ink-red-6'), download)]);
    const when = (v) => (v ? frappe.datetime.str_to_user(v) : '-');
    const details = [kv(__('Started by'), frappe.utils.escape_html(frappe.user.full_name(j.partner))),
      kv(__('Started on'), when(j.submitted_at)), kv(__('Finished on'), when(j.finished_at))];
    const word = { created: __('Created'), merged: __('Updated'), validated: __('Passed'), failed: __('Failure') };
    const body = rows.map((r) => {
      const failed = r.action === 'failed';
      const message = failed ? frappe.utils.escape_html(r.error_message || '')
        : r.record_name ? `<a href="${frappe.utils.get_form_link('CRM Lead', r.record_name)}">${frappe.utils.escape_html(r.record_name)}</a>` : '';
      return `<tr>
          <td class="diw-import-log-cell-row whitespace-nowrap text-sm align-top border-b py-2 px-4">${cint(r.record_index) + 2}</td>
          <td class="text-sm align-top border-b py-2 px-4">${frappe.ui.badge.html({ label: word[r.action] || r.action, theme: failed ? 'red' : 'green' })}</td>
          <td class="text-sm align-top border-b py-2 px-4 break-words">${message}</td></tr>`;
    }).join('');
    const $log = $(`
      <div>
        ${tatva_li_head(__('Import Log')).prop('outerHTML')}
        <div class="diw-import-log-summary mb-4"><div class="diw-import-log-summary-grid flex gap-8">
          <div class="diw-log-kv-col flex-1 min-w-0"><div class="diw-log-kv-caption text-xs text-ink-gray-6 mb-1">${__('Result')}</div><table class="diw-log-kv-table w-full">${result.join('')}</table></div>
          <div class="diw-log-kv-col flex-1 min-w-0"><div class="diw-log-kv-caption text-xs text-ink-gray-6 mb-1">${__('Details')}</div><table class="diw-log-kv-table w-full">${details.join('')}</table></div>
        </div></div>
        <div class="diw-import-log-filter-row mt-3 mb-3 flex items-center justify-between gap-2"><div class="diw-import-log-filter-tabs"></div></div>
        ${rows.length ? `<table class="diw-import-log-table w-full"><thead><tr class="text-ink-gray-6">
            <th class="text-sm-semibold border-b py-2 px-4" width="10%">${__('Row')}</th>
            <th class="text-sm-semibold border-b py-2 px-4" width="14%">${__('Status')}</th>
            <th class="text-sm-semibold border-b py-2 px-4" width="76%">${__('Message')}</th></tr></thead><tbody>${body}</tbody></table>`
          : `<div class="diw-import-log-empty-state">${frappe.ui.empty_state.html({ icon: 'inbox', title: __('No rows to show'), css_class: 'min-h-32' })}</div>`}
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
      on_change: (value) => tatva_li_log(frm, $c.empty(), job, value),
    }).el);
    frappe.utils.bind_actions_with_object($log, {
      download_refused: () => open_url_post('/api/method/' + TATVA_LI_API + 'download_refused_rows', { lead_import: frm.doc.name, job }),
    });
    $c.empty().append($log);
  });
}

// The footer, as Data Import's: Back; on the step reached, Save while edited, else the stage's action; elsewhere Next.
function tatva_li_footer(frm) {
  const w = frm._li_wizard;
  const action = w.step === w.reached && tatva_li_stage(frm);
  const label = action && { validate: w.step === 1 ? __('Validate') : __('Retry'), import: __('Import'), stop: __('Stop') }[action.stage];
  let $right = null;
  if (action && frm.is_dirty()) $right = frappe.ui.button({ label: __('Save'), variant: 'solid', onclick: () => frm.save() });
  else if (action) $right = frappe.ui.button({ label, variant: 'solid', icon_left: action.stage === 'validate' && w.step > 1 ? 'refresh-cw' : '', onclick: action.run });
  else if (w.step < TATVA_LI_LAST) $right = frappe.ui.button({ label: __('Next'), icon_right: 'arrow-right', onclick: () => tatva_li_next(frm) });
  w.$left.empty().append(w.step > 0 ? frappe.ui.button({ label: __('Back'), icon_left: 'arrow-left', onclick: () => tatva_li_go(frm, w.step - 1) }) : []);
  w.$right.empty().append($right || []);
}

// Next saves first when there is anything to save, then moves as far as the saved state allows.
function tatva_li_next(frm) {
  const target = frm._li_wizard.step + 1;
  if (!frm.is_new() && !frm.is_dirty()) return tatva_li_go(frm, target);
  return frm.save().then(() => !frm.is_dirty() && tatva_li_go(frm, Math.min(target, frm._li_wizard.reached))); // frappe resolves a refused save too, and it leaves the form dirty
}

// Move to step `i`, or say what to do first when it is not reached yet.
function tatva_li_go(frm, i) {
  const w = frm._li_wizard;
  if (i > w.reached) {
    frappe.show_alert({ message: tatva_li_next_step(frm), indicator: 'orange' });
    return;
  }
  w.step = Math.max(0, i);
  tatva_li_render(frm);
  tatva_li_progress_from_job(frm);
}
