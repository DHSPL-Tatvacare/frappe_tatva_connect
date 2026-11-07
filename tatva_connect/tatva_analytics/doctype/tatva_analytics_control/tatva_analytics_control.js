// Run now and Rebuild queue this job on the long lane; each run lands in the Run Log.
const ANALYTICS_STATUS = { Running: 'blue', Success: 'green', Failed: 'red' };

frappe.ui.form.on('Tatva Analytics Control', {
  setup(frm) {
    // Every time here is the system's own; the zone line under each date would misalign the rows.
    frm.fields.forEach((field) => { if (field.df.fieldtype === 'Datetime') field.df.hide_timezone = 1; });
  },
  refresh(frm) {
    if (frm.doc.last_status) frm.page.set_indicator(__(frm.doc.last_status), ANALYTICS_STATUS[frm.doc.last_status]);
    if (frm.is_new() || !frm.doc.enabled) return;
    frm.add_custom_button(__('Run now'), () => analytics_queue(frm, 'run_now'));
    frm.add_custom_button(__('Rebuild'), () => frappe.confirm(
      __('Read the window of {0} again from its sources? Rows already in its table are updated in place; it runs on the long queue.', [frm.doc.name]),
      () => analytics_queue(frm, 'rebuild'),
    ));
  },
});

function analytics_queue(frm, method) {
  frappe.call({
    method: `tatva_connect.tatva_analytics.engine.${method}`,
    args: { job: frm.doc.name },
    callback: () => frappe.show_alert({ message: __('Queued. Its run appears in the Run Log.'), indicator: 'blue' }),
  });
}
