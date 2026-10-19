// CRM Telephony Account form: the shared Webhook menu + URL banner (tatva_webhook_actions), shown once a provider is set.

frappe.ui.form.on('CRM Telephony Account', {
  refresh(frm) {
    if (!frm.doc.provider) return;

    tatva_webhook_actions(frm, 'webhook_token', TELEPHONY_WEBHOOK_BANNER);
    if (!frm.is_new()) {
      tatva_validate_token(frm, 'Token is accepted', 'Token is not accepted', null, {
        method: 'tatva_connect.telephony.discovery.validate_token', freeze_message: __('Asking the provider…'),
      });
    }
  },

  // re-fetch the banner the moment the token changes (generate / manual edit)
  webhook_token(frm) {
    telephony_show_webhook_urls(frm);
  },
});

const TELEPHONY_WEBHOOK_BANNER = {
  single_label: 'Register this webhook on the Acefone dashboard',
  multi_label: 'Register these webhooks on the Acefone dashboard',
  register_hint: 'Acefone → Webhooks → Add: Channel Voice, Content Type JSON, keep the default payload template, Retry webhook ON. Outbound answered has no Acefone trigger and is not listed.',
};

function telephony_show_webhook_urls(frm) {
  if (!frm.doc.provider) return;
  tatva_render_webhook_urls(frm, TELEPHONY_WEBHOOK_BANNER);
}
