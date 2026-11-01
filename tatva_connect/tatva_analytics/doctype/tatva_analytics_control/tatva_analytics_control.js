// Run now and Rebuild queue this job on the long lane; each run lands in the Run Log.
const ANALYTICS_STATUS = { Running: 'blue', Success: 'green', Failed: 'red' };

frappe.ui.form.on('Tatva Analytics Control', {
  refresh(frm) {
    if (frm.doc.last_status) frm.page.set_indicator(__(frm.doc.last_status), ANALYTICS_STATUS[frm.doc.last_status]);
    if (frm.is_new() || !frm.doc.enabled) return;
    frm.add_custom_button(__('Run now'), () => analytics_queue(frm, 'run_now'));
    frm.add_custom_button(__('Rebuild'), () => frappe.confirm(
      __('Reload the window of {0} from scratch, as saved on this job? It runs on the long queue, one day at a time.', [frm.doc.name]),
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
