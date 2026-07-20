// Desk Client Script — CRM Lead Import (/app/crm-lead-import). Everything shown is decided server-side.
//   1) Contract  -> filtered to the grains this operator holds, via the contract_query Link query.
//   2) Grain     -> read off the contract and painted as a headline, the same way the account forms do.
//   3) Columns   -> the grid's two dropdowns come from the ONE mapping seam, fed by tatva_set_grid_* .
//   4) Buttons   -> Download Template, Validate (mandatory dry run), Import (only once Validated), Results.
// The client filters nothing and decides nothing; a mapping it saves is re-checked on the server anyway.

frappe.ui.form.on('CRM Lead Import', {
  onload(frm) {
    frm.set_query('contract', () => ({ query: 'tatva_connect.lead_import.api.contract_query' }));
  },

  refresh(frm) {
    tatva_li_headline(frm);
    tatva_li_section_options(frm);
    tatva_li_buttons(frm);
  },

  contract(frm) {
    tatva_li_headline(frm);
  },

  // A new file invalidates the old mapping; the server rebuilds the grid from the new header.
  import_file(frm) {
    if (frm.is_new() || !frm.doc.import_file) return;
    frappe.call({
      method: 'tatva_connect.lead_import.api.read_columns',
      args: { lead_import: frm.doc.name },
      freeze: true,
      freeze_message: __('Reading the file…'),
      callback: () => frm.reload_doc(),
    });
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

// The grain is never typed — it is read off the contract so the operator sees what will be stamped.
function tatva_li_headline(frm) {
  frm.dashboard.clear_headline();
  if (frm.is_new() || !frm.doc.contract) return;
  frappe.call({
    method: 'tatva_connect.lead_import.api.describe_grain',
    args: { lead_import: frm.doc.name },
    callback(r) {
      const g = (r && r.message) || {};
      const cell = (label, value) =>
        '<b>' + frappe.utils.escape_html(label) + ':</b> ' + frappe.utils.escape_html(value || '—');
      frm.dashboard.set_headline(
        [
          cell(__('Vertical'), g.vertical),
          cell(__('Group'), g.group),
          cell(__('Programme'), g.program),
          cell(__('Source'), g.source),
          cell(__('Writable fields'), String(g.writable_count || 0)),
        ].join(' &nbsp;·&nbsp; ')
      );
    },
  });
}

// Section list: column-wide, from the section brain. A section added later needs no change here.
function tatva_li_section_options(frm) {
  if (frm.is_new()) return;
  const grid = frm.fields_dict.columns && frm.fields_dict.columns.grid;
  frappe.call({
    method: 'tatva_connect.lead_import.api.list_sections',
    args: { lead_import: frm.doc.name },
    callback(r) {
      tatva_set_grid_column_options(grid, 'target_table', (r && r.message) || []);
    },
  });
}

// Field list: per-row, scoped to that row's section AND the contract's grain, resolved server-side.
function tatva_li_field_options(frm, cdn) {
  const row = (frm.doc.columns || []).find((c) => c.name === cdn);
  if (!row || !row.target_table) return;
  const grid = frm.fields_dict.columns && frm.fields_dict.columns.grid;
  frappe.call({
    method: 'tatva_connect.lead_import.api.list_fields',
    args: { lead_import: frm.doc.name, section: row.target_table },
    callback(r) {
      const data = (r && r.message) || [];
      tatva_set_grid_row_options(grid, cdn, 'target_field', data);
      if (!data.length) {
        frappe.show_alert({
          message: __('The contract on this import ticks no writable field in that section.'),
          indicator: 'orange',
        });
      }
    },
  });
}

function tatva_li_buttons(frm) {
  if (frm.is_new()) return;

  if (frm.doc.contract) {
    frm.add_custom_button(__('Excel'), () => tatva_li_template(frm, 'xlsx'), __('Download Template'));
    frm.add_custom_button(__('CSV'), () => tatva_li_template(frm, 'csv'), __('Download Template'));
  }

  if ((frm.doc.columns || []).length) {
    frm.add_custom_button(__('Validate'), () => tatva_li_run(frm, 'tatva_connect.lead_import.api.start_validation',
      __('Queueing the validation run…'))).addClass('btn-primary');
  }

  if (frm.doc.status === 'Validated') {
    frm.add_custom_button(__('Import'), () => {
      frappe.confirm(
        __('{0} rows will be written. {1} rows were refused and will be skipped.',
          [frm.doc.valid_rows, frm.doc.invalid_rows]),
        () => tatva_li_run(frm, 'tatva_connect.lead_import.api.start_import', __('Queueing the import…'))
      );
    }).addClass('btn-primary');
  }

  const job = frm.doc.import_job || frm.doc.dry_run_job;
  if (job) {
    frm.add_custom_button(__('Row Results'), () => frappe.set_route('List', 'CRM Bulk Job Result', { job }));
    frm.add_custom_button(__('Job Progress'), () => frappe.set_route('Form', 'CRM Bulk Job', job));
  }
}

function tatva_li_run(frm, method, message) {
  frappe.call({
    method,
    args: { lead_import: frm.doc.name },
    freeze: true,
    freeze_message: message,
    callback() {
      frappe.show_alert({ message: __('Queued. Progress is reported on the job.'), indicator: 'blue' });
      frm.reload_doc();
    },
  });
}

function tatva_li_template(frm, fmt) {
  open_url_post('/api/method/tatva_connect.lead_import.api.download_template',
    { lead_import: frm.doc.name, fmt });
}
