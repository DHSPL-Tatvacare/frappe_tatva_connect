// Desk Client Script — CRM Facebook App: validate the stored token, and discover this app's Pages and forms.

frappe.ui.form.on('CRM Facebook App', {
  refresh(frm) {
    if (frm.is_new()) return;

    tatva_validate_token(frm, 'Token will carry a discovery', 'Token will not carry a discovery');

    // Discovery is app-wide, so it says what it reads before it reads it.
    frm.add_custom_button(__('Refresh From Facebook'), () => {
      frappe.confirm(tatva_fb_discovery_prompt(), () => tatva_fb_discover(frm.doc.name));
    });
  },
});
