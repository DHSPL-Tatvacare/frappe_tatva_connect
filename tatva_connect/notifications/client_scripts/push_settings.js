// Desk Client Script — CRM Push Settings (/app/crm-push-settings).
// The two buttons answer the only question this form raises: are these creds real, and would a push land?

frappe.ui.form.on('CRM Push Settings', {
  refresh(frm) {
    frm.add_custom_button(__('Validate Credentials'), () => {
      frappe.call({
        method: 'tatva_connect.notifications.api.validate_push_config',
        freeze: true,
        freeze_message: __('Asking Firebase…'),
        callback: (r) =>
          tatva_show_check_report(r.message || {}, {
            ok_title: 'Push is configured',
            fail_title: 'Push is not ready',
          }),
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

