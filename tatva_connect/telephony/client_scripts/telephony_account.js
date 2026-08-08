// Desk Client Script — CRM Telephony Account form (/app/crm-telephony-account).
// Webhook helper affordances, mirroring the WhatsApp Account form, so an operator never has
// to hand-craft a secret or assemble URLs:
//   1) "Generate Webhook Token" -> fills webhook_token with a random secret.
//   2) "Copy Webhook URLs" + a banner listing the four ready-to-register inbound URLs
//      https://<host>/webhooks/telephony/<provider>/<token>/<event> (one per Acefone trigger).
// The token is a Password field (masked as `***` after save), so the URLs are fetched from the
// server (tatva_connect.webhooks.urls.get_account_webhook_urls), never built from frm.doc.<token>.
// The token generator + URL-banner renderer are the shared Desk helpers (public/js/tatva_connect.bundle.js).
// Shown once a provider is set. The token both authenticates the caller and identifies the
// receiving account (telephony.routing.account_by_webhook_token). No fork — a Client Script.

frappe.ui.form.on('CRM Telephony Account', {
  refresh(frm) {
    if (!frm.doc.provider) return;

    frm.add_custom_button(__('Generate Webhook Token'), () => {
      frm.set_value('webhook_token', tatva_webhook_random_token());
      frappe.show_alert({ message: __('Webhook token generated — Save to apply.'), indicator: 'green' });
    });

    if (frm.doc.webhook_token) {
      frm.add_custom_button(__('Copy Webhook URLs'), () => {
        const urls = frm.__tatva_webhook_urls || [];
        if (urls.length) frappe.utils.copy_to_clipboard(urls.join('\n'));
      });
    }

    telephony_show_webhook_urls(frm);
  },

  // re-fetch the banner the moment the token changes (generate / manual edit)
  webhook_token(frm) {
    telephony_show_webhook_urls(frm);
  },
});

function telephony_show_webhook_urls(frm) {
  if (!frm.doc.provider) return;
  tatva_render_webhook_urls(frm, {
    single_label: 'Register this webhook on the Acefone dashboard',
    multi_label: 'Register these webhooks on the Acefone dashboard',
    register_hint: 'Acefone → Webhooks → Add: Channel Voice, Content Type JSON, keep the default payload template, Retry webhook ON. Outbound answered has no Acefone trigger and is not listed.',
  });
}
