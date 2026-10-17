// WhatsApp Account form: Refresh From WhatsApp, plus the shared Webhook menu + URL banner (tatva_webhook_actions) for WATI accounts.

frappe.ui.form.on('WhatsApp Account', {
  refresh(frm) {
    // Upstream's Subscribe button is a Meta Graph call (needs version + business_id); every other provider subscribes by registering the URL on its own dashboard.
    if (frm.doc.custom_provider !== 'Meta') frm.remove_custom_button(__('Subscribe App to Webhooks'));

    if (!frm.is_new()) {
      frm.add_custom_button(__('Refresh From WhatsApp'), () => tatva_wa_refresh_templates(frm.doc.name));
    }

    if (frm.doc.custom_provider !== 'WATI') return;

    tatva_webhook_actions(frm, 'custom_webhook_token', WHATSAPP_WEBHOOK_BANNER);
  },

  // re-fetch the banner the moment the token changes (generate / manual edit)
  custom_webhook_token(frm) {
    whatsapp_show_webhook_url(frm);
  },
});

function whatsapp_show_webhook_url(frm) {
  if (frm.doc.custom_provider !== 'WATI') return;
  tatva_render_webhook_urls(frm, WHATSAPP_WEBHOOK_BANNER);
}

const WHATSAPP_WEBHOOK_BANNER = {
  single_label: 'Register this webhook on the WATI dashboard',
  multi_label: 'Register these webhooks on the WATI dashboard',
  register_hint: 'WATI → Webhook → Add Webhook → paste this URL; on a tenant with several numbers, select only this account\'s number.',
};
