// Desk Client Script — CRM Push Settings (/app/crm-push-settings).
// The three Firebase secrets are Password fields, which hold only asterisks once saved, so the stock eye
// toggle reveals nothing; the shared helper (public/js/tatva_connect.bundle.js) reads the real value back.
// The two buttons answer the only question this form raises: are these creds real, and would a push land?

frappe.ui.form.on('CRM Push Settings', {
  refresh(frm) {
    tatva_enable_secret_reveal(frm, ['service_account_json', 'web_api_key', 'vapid_key']);

    frm.add_custom_button(__('Validate Credentials'), () => {
      frappe.call({
        method: 'tatva_connect.notifications.api.validate_push_config',
        freeze: true,
        freeze_message: __('Asking Firebase…'),
        callback: (r) => tatva_show_push_report(r.message || {}),
      });
    });

    frm.add_custom_button(__('Send Test Push To Me'), () => {
      frappe.call({
        method: 'tatva_connect.notifications.api.send_test_push',
        freeze: true,
        freeze_message: __('Sending…'),
        callback: (r) => {
          const res = r.message || {};
          frappe.msgprint({
            title: res.ok ? __('Test push sent') : __('Nothing to push to'),
            indicator: res.ok ? 'green' : 'orange',
            message: frappe.utils.escape_html(res.detail || ''),
          });
        },
      });
    });
  },
});

// The report is a list of checks, each pass/fail with the reason — an operator should never have to read a log to find out which field is wrong.
function tatva_show_push_report(report) {
  const rows = (report.checks || [])
    .map((c) => {
      const mark = c.passed ? '✅' : '❌';
      const detail = c.detail
        ? ` <span style="color:var(--text-muted)">— ${frappe.utils.escape_html(c.detail)}</span>`
        : '';
      return `<div style="margin-bottom:4px">${mark} ${frappe.utils.escape_html(c.label)}${detail}</div>`;
    })
    .join('');
  frappe.msgprint({
    title: report.ok ? __('Push is configured') : __('Push is not ready'),
    indicator: report.ok ? 'green' : 'red',
    message: rows || __('Nothing to check — the form is blank.'),
  });
}
