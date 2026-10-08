// Desk Client Script — WhatsApp Account form (Frappe Desk, /app/whatsapp-account).
// WhatsApp webhook setup affordances so an operator never has to hand-craft a secret or assemble a URL:
//   1) Webhook > Generate Token -> fills custom_webhook_token with a random secret.
//   2) Webhook > Copy URL + a headline banner showing the ready-to-register inbound
//      URL  https://<host>/webhooks/whatsapp/<token>.
//   3) "Refresh From WhatsApp" -> this account's templates, through the shared tatva_wa_refresh_templates.
// The token is a Password field (masked as `***` after save), so the URL is fetched from the
// server (tatva_connect.webhooks.urls.get_account_webhook_urls), never built from frm.doc.<token>.
// The token generator + URL-banner renderer are the shared Desk helpers (public/js/tatva_connect.bundle.js).
// WATI-only: everything is gated on custom_provider === 'WATI', so other providers see nothing extra.
// No fork — a Client Script override on an upstream doctype.

frappe.ui.form.on('WhatsApp Account', {
  refresh(frm) {
    // Upstream's Subscribe button is a Meta Graph call (needs version + business_id); every other provider subscribes by registering the URL on its own dashboard.
    if (frm.doc.custom_provider !== 'Meta') frm.remove_custom_button(__('Subscribe App to Webhooks'));

    if (!frm.is_new()) {
      frm.add_custom_button(__('Refresh From WhatsApp'), () => tatva_wa_refresh_templates(frm.doc.name));
    }

    if (frm.doc.custom_provider !== 'WATI') return;

    frm.add_custom_button(__('Generate Token'), () => {
      frappe.confirm(tatva_webhook_token_prompt(), () => {
        frm.set_value('custom_webhook_token', tatva_webhook_random_token());
        frappe.show_alert({ message: __('Webhook token generated — Save to apply.'), indicator: 'green' });
      });
    }, __('Webhook'));

    if (frm.doc.custom_webhook_token) {
      frm.add_custom_button(__('Copy URL'), () => {
        const urls = frm.__tatva_webhook_urls || [];
        if (urls.length) frappe.utils.copy_to_clipboard(urls.join('\n'));
      }, __('Webhook'));
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
    register_hint: 'WATI → Webhook → Add Webhook → paste this URL; on a tenant with several numbers, select only this account\'s number.',
  });
}
