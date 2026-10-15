// Desk Client Script — CRM Lead Import, on frappe's Data Import pattern: one action per stage, realtime progress, one reload.

const TATVA_LI_API = 'tatva_connect.lead_import.api.';

frappe.ui.form.on('CRM Lead Import', {
  setup(frm) {
    frappe.realtime.on('bulk_job_progress', (data) => tatva_li_progress(frm, data));
    frappe.realtime.on('lead_import_refresh', ({ lead_import }) => {
      if (lead_import === frm.doc.name) frm.reload_doc();
    });
    $(frm.wrapper).on('dirty', () => frm._li_wizard && tatva_li_wizard_show(frm));
  },

  onload(frm) {
    frm.set_query('contract', () => ({ query: TATVA_LI_API + 'contract_query' }));
  },

  refresh(frm) {
    tatva_li_section_options(frm);
    tatva_li_templates(frm);
    tatva_li_progress_from_job(frm);
    tatva_li_preview(frm);
    tatva_li_wizard(frm);
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
  if (frm.is_new()) return __('Pick the contract, then press Next.');
  if (!(d.__onload || {}).has_source) return __('Attach the CSV or XLSX file, or give a Google Sheets URL. Download Template gives the columns this contract accepts.');
  return {
    Draft: __('Check each column maps to the right field, or tick Skip. Then validate — nothing is written until you import.'),
    Validating: __('Validating every row. Nothing is saved to the database in this path.'),
    Validated: __('{0} rows passed, {1} refused. Choose the Bulk Lane, then import the rows that passed.', [d.valid_rows, d.invalid_rows]),
    'Validation Failed': __('No row passed. Open Import Results, fix the file and attach it again.'),
    Importing: __('Importing.'),
    Imported: __('{0} rows imported.', [d.valid_rows]),
    'Partially Imported': __('{0} rows imported, {1} refused. Open Import Results for the reasons.', [d.valid_rows, d.invalid_rows]),
    'Import Failed': __('The import stopped. Open the Import Job for the reason, then validate again to retry.'),
    Cancelled: __('The import was cancelled. Validate again to retry.'),
  }[d.status];
}

// Until a file is attached, the header's Download Template menu: every field the contract takes, or a chosen few.
function tatva_li_templates(frm) {
  if (frm.is_new() || (frm.doc.__onload || {}).has_source) return;
  frm.add_custom_button(__('All Fields'), () => tatva_li_download(frm), __('Download Template'));
  frm.add_custom_button(__('Choose Fields'), () => tatva_li_choose_fields(frm), __('Download Template'));
}

// The one action this stage offers, as the server names it, for the wizard's footer; none when there is nothing to do.
function tatva_li_stage(frm) {
  const d = frm.doc;
  const stage = (d.__onload || {}).next_stage;
  if (stage === 'validate') return { label: __('Validate'), run: () => tatva_li_run(frm, 'start_validation') };
  if (stage === 'import') return { label: __('Import'), run: () => tatva_li_confirm_import(frm) };
  if (stage === 'stop') return { label: __('Stop'), run: () => frappe.confirm(tatva_li_stop_warning(d), () => tatva_li_run(frm, 'stop_import')) };
  if (d.import_job || d.dry_run_job) {
    return { label: __('View Results'), run: () => frappe.set_route('List', 'CRM Bulk Job Result', { job: d.import_job || d.dry_run_job }) };
  }
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

// The worker announces each committed chunk; draw it only for this import's own running job.
function tatva_li_progress(frm, { job, processed, total }) {
  const d = frm.doc;
  const running = tatva_li_running_job(d);
  if (!running || job !== running) return;
  const title = d.status === 'Validating' ? __('Validating') : __('Importing');
  frm.dashboard.show_progress(title, (processed * 100) / total, __('{0} of {1} rows', [processed, total]));
}

// A reload redraws the bar from the running job's own row; the event above is only the fast path.
function tatva_li_progress_from_job(frm) {
  const job = tatva_li_running_job(frm.doc);
  if (!job) return;
  frappe.db.get_value('CRM Bulk Job', job, ['processed', 'total']).then(({ message: m }) => {
    if (m && m.total) tatva_li_progress(frm, { job, processed: m.processed, total: m.total });
  });
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

// The wizard's steps, each the sections it shows and is named by; the fields never move, the other steps' sections hide.
const TATVA_LI_STEPS = [['grain_section'], ['file_section'], ['mapping_section'], ['result_section'], ['import_section', 'notifications_section']];
const TATVA_LI_LAST = TATVA_LI_STEPS.length - 1;

// The furthest step this import's state has reached, read off what the server already returns.
function tatva_li_reached(frm) {
  const d = frm.doc;
  if (frm.is_new()) return 0;
  if (!(d.__onload || {}).has_source) return 1;
  if (d.status === 'Draft') return 2;
  if (['Validating', 'Validation Failed'].includes(d.status)) return 3;
  return TATVA_LI_LAST;
}

// Frappe 16.52's Data Import wizard look, built once per form; the form's layout is reused across imports.
function tatva_li_wizard(frm) {
  const reached = tatva_li_reached(frm);
  const w = frm._li_wizard || (frm._li_wizard = tatva_li_wizard_build(frm));
  if (w.docname !== frm.doc.name) w.step = null; // another import opened; a first save moves on through the rule below
  w.docname = frm.doc.name;
  // Land on the furthest step, follow the state when it moves forward, otherwise stay where the operator is.
  w.step = w.step == null || reached > w.reached ? reached : Math.min(w.step, reached);
  w.reached = reached;
  tatva_li_wizard_show(frm);
}

// Frappe's own Data Import shell (its desk classes): gray page, the steppers, the card holding the form layout, a footer.
function tatva_li_wizard_build(frm) {
  const w = { step: null, reached: 0, docname: null };
  const steps = TATVA_LI_STEPS.map((sections) => ({ label: __(frm.fields_dict[sections[0]].df.label) }));
  w.stepper = new frappe.ui.Stepper({
    steps,
    label: __('Import steps'),
    is_locked: (i) => i > w.reached,
    is_completed: (i) => i < w.reached || (i === TATVA_LI_LAST && frm.doc.import_job && !(frm.doc.__onload || {}).next_stage),
    on_step_click: (i) => tatva_li_go(frm, i),
    on_locked_click: (i) => tatva_li_go(frm, i),
  });
  w.compact = new frappe.ui.Stepper({ steps, label: __('Import steps'), compact: true });
  frm.page.wrapper.addClass('data-import-custom-shell'); // frappe's rule: hides the header Save, the footer acts instead
  frm.page.main.addClass('data-import-custom-page flex flex-col flex-1');
  frm.page.main.closest('.layout-main-section-wrapper').addClass('data-import-custom-wrapper');
  const $shell = $(`
    <div class="data-import-custom-ui flex justify-center w-full">
      <div class="diw-shell flex flex-col w-full min-w-0 gap-5">
        <div class="diw-stepper-wrap shrink-0 w-full"></div>
        <div class="diw-card flex flex-col w-full min-w-0 overflow-hidden rounded-lg shadow-sm border bg-surface-base">
          <div class="diw-mobile-step-header shrink-0 pt-4 px-4"></div>
          <div class="diw-step-content flex-1 min-w-0 w-full py-2"></div>
          <div class="diw-status-area shrink-0 px-5 pb-4"></div>
          <div class="diw-footer flex items-center justify-between shrink-0 w-full border-t mt-auto py-4 px-5">
            <div class="diw-footer-left"></div>
            <div class="diw-footer-right flex items-center gap-2"></div>
          </div>
        </div>
      </div>
    </div>`).prependTo(frm.page.main);
  $shell.find('.diw-stepper-wrap').append(w.stepper.$el);
  $shell.find('.diw-mobile-step-header').append(w.compact.$el);
  $shell.find('.diw-step-content').append(frm.layout.wrapper); // frappe's page rule hides the emptied std form, with its sidebar and timeline
  w.$status = $shell.find('.diw-status-area');
  w.$left = $shell.find('.diw-footer-left');
  w.$right = $shell.find('.diw-footer-right');
  return w;
}

// Show only the current step's sections, and redraw the steppers, the next-step line and the footer.
function tatva_li_wizard_show(frm) {
  const w = frm._li_wizard;
  TATVA_LI_STEPS.forEach((sections, i) => frm.toggle_display(sections, i === w.step));
  w.stepper.set_current(w.step);
  w.compact.set_current(w.step);
  // The next-step line in the status box frappe's wizard draws its form message in.
  w.$status.empty().append($('<div class="p-4 rounded border bg-surface-gray-2">').text(tatva_li_next_step(frm) || ''));
  w.$left.empty().append(w.step > 0 ? frappe.ui.button({ label: __('Back'), icon_left: 'arrow-left', onclick: () => tatva_li_go(frm, w.step - 1) }) : []);
  w.$right.empty().append(tatva_li_footer_action(frm) || []);
}

// The footer's one button, as frappe's wizard: on a step with an action, Save while edited, else the action; elsewhere Next.
function tatva_li_footer_action(frm) {
  const w = frm._li_wizard;
  const stage = w.step === w.reached && tatva_li_stage(frm);
  if (stage && frm.is_dirty()) return frappe.ui.button({ label: __('Save'), variant: 'solid', onclick: () => frm.save() });
  if (stage) return frappe.ui.button({ label: stage.label, variant: 'solid', onclick: stage.run });
  if (w.step < TATVA_LI_LAST) return frappe.ui.button({ label: __('Next'), icon_right: 'arrow-right', onclick: () => tatva_li_next(frm) });
  return null;
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
  tatva_li_wizard_show(frm);
  frappe.utils.scroll_to(frm.page.main);
}
