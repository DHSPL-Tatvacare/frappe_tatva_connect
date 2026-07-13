// Desk Client Script — WhatsApp Account form (Frappe Desk, /app/whatsapp-account).
// WhatsApp webhook setup affordances so an operator never has to hand-craft a secret or assemble a URL:
//   1) "Generate Webhook Token" button  -> fills custom_webhook_token with a random secret.
//   2) "Copy Webhook URL" button + a headline banner showing the ready-to-register inbound
//      URL  https://<host>/webhooks/whatsapp/wati/<token>.
// The token is a Password field (masked as `***` after save), so the URL is fetched from the
// server (tatva_connect.webhooks.urls.get_account_webhook_urls), never built from frm.doc.<token>.
// The token generator + URL-banner renderer are the shared Desk helpers (public/js/tatva_connect.bundle.js).
// WATI-only: everything is gated on custom_provider === 'WATI', so other providers see nothing extra.
// No fork — a Client Script override on an upstream doctype.

frappe.ui.form.on('WhatsApp Account', {
  refresh(frm) {
    // Upstream's Subscribe button is a Meta Graph call (needs version + business_id); every other provider subscribes by registering the URL on its own dashboard.
    if (frm.doc.custom_provider !== 'Meta') frm.remove_custom_button(__('Subscribe App to Webhooks'));

    // Both secrets on this form get the same working eye toggle, whatever the provider.
    tatva_enable_secret_reveal(frm, ['token', 'custom_webhook_token', 'custom_webhook_token_previous']);

    if (frm.doc.custom_provider !== 'WATI') return;

    frm.add_custom_button(__('Generate Webhook Token'), () => {
      frm.set_value('custom_webhook_token', tatva_webhook_random_token());
      frappe.show_alert({ message: __('Webhook token generated — Save to apply.'), indicator: 'green' });
    });

    if (frm.doc.custom_webhook_token) {
      frm.add_custom_button(__('Copy Webhook URL'), () => {
        const urls = frm.__tatva_webhook_urls || [];
        if (urls.length) frappe.utils.copy_to_clipboard(urls.join('\n'));
      });
    }

    whatsapp_show_webhook_url(frm);
  },

  // re-fetch the banner the moment the token changes (generate / manual edit)
  custom_webhook_token(frm) {
    whatsapp_show_webhook_url(frm);
  },
});

function whatsapp_show_webhook_url(frm) {
  if (frm.doc.custom_provider !== 'WATI') return;
  tatva_render_webhook_urls(frm, {
    single_label: 'Register this webhook on the WATI dashboard',
    multi_label: 'Register these webhooks on the WATI dashboard',
    register_hint: 'WATI → Webhooks → paste this URL; one URL receives every event.',
  });
}
