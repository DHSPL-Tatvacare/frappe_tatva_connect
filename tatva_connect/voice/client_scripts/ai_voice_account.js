// Desk Client Script — CRM AI Voice Account form (/app/crm-ai-voice-account).
// The same webhook affordances the WhatsApp and Telephony account forms carry, so an operator never
// hand-crafts a secret or assembles a URL:
//   1) "Generate Webhook Token" -> fills webhook_token with a random secret.
//   2) "Copy Webhook URLs" + a banner showing the one ready-to-register URL
//      https://<host>/webhooks/voice/<token>
// The token is a Password field (masked as `***` after save), so the URL is fetched from the server
// (tatva_connect.webhooks.urls.get_account_webhook_urls), never built from frm.doc.webhook_token.
// The token generator + URL-banner renderer are the shared Desk helpers (public/js/tatva_connect.bundle.js).

frappe.ui.form.on('CRM AI Voice Account', {
  refresh(frm) {
    // The webhook affordances belong to a provider, so they wait for one — and SAY so: withdrawing three
    // controls in silence reads as a broken form rather than as an unanswered question.
    if (!frm.doc.provider) {
      frm.dashboard.clear_headline();
      frm.dashboard.set_headline(
        __('Pick a provider to set up this account’s webhook — the token and the URL to register belong to it.')
      );
      return;
    }

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

    voice_show_webhook_urls(frm);
  },

  // re-fetch the banner the moment the token changes (generate / manual edit)
  webhook_token(frm) {
    voice_show_webhook_urls(frm);
  },
});

function voice_show_webhook_urls(frm) {
  // The `refresh` above already says why when there is no provider; painting a second headline here would
  // overwrite that sentence with nothing.
  if (!frm.doc.provider) return;
  tatva_render_webhook_urls(frm, {
    single_label: 'Register this webhook on the voice provider',
    multi_label: 'Register these webhooks on the voice provider',
    register_hint:
      'Bolna → the agent that places these calls → Webhook URL. One URL carries every execution event. Without it a call is placed and its outcome never returns, so the journey waiting on it does not move.',
  });
}
