// CRM AI Voice Account form: the shared Webhook menu + URL banner (tatva_webhook_actions), shown once a provider is set.

frappe.ui.form.on('CRM AI Voice Account', {
  refresh(frm) {
    // No provider yet: say so, rather than silently hiding the Webhook menu.
    if (!frm.doc.provider) {
      frm.dashboard.clear_headline();
      frm.dashboard.set_headline(
        __('Pick a provider to set up this account’s webhook — the token and the URL to register belong to it.')
      );
      return;
    }

    tatva_webhook_actions(frm, 'webhook_token', VOICE_WEBHOOK_BANNER);
  },

  // re-fetch the banner the moment the token changes (generate / manual edit)
  webhook_token(frm) {
    voice_show_webhook_urls(frm);
  },
});

function voice_show_webhook_urls(frm) {
  // No provider: keep refresh's headline instead of overwriting it.
  if (!frm.doc.provider) return;
  tatva_render_webhook_urls(frm, VOICE_WEBHOOK_BANNER);
}

const VOICE_WEBHOOK_BANNER = {
  single_label: 'Register this webhook on the voice provider',
  multi_label: 'Register these webhooks on the voice provider',
  register_hint:
    'Bolna → the agent that places these calls → Webhook URL. One URL carries every execution event. Without it a call is placed and its outcome never returns, so the journey waiting on it does not move.',
};
