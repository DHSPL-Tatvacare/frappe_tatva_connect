// Desk Client Script — CRM Facebook App: validate the stored token, and discover this app's Pages and forms.

frappe.ui.form.on('CRM Facebook App', {
  refresh(frm) {
    if (frm.is_new()) return;

    frm.add_custom_button(__('Validate Token'), () => {
      frappe.call({
        method: 'tatva_connect.lead_sync.api.validate_token',
        args: { doctype: frm.doctype, name: frm.doc.name },
        freeze: true,
        freeze_message: __('Asking Facebook…'),
        callback: (r) =>
          tatva_show_check_report(r.message || {}, {
            ok_title: 'Token will carry a crawl',
            fail_title: 'Token will not carry a crawl',
          }),
      });
    });

    // Discovery is app-wide, so it says what it reads before it reads it.
    frm.add_custom_button(__('Refresh From Facebook'), () => {
      frappe.confirm(tatva_fb_discovery_prompt(), () => tatva_fb_discover(frm.doc.name));
    });
  },
});
