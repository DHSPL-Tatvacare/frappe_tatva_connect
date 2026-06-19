// Desk Client Script — CRM Telephony Account form (/app/crm-telephony-account).
// Webhook helper affordances, mirroring the WhatsApp Account form, so an operator never has
// to hand-craft a secret or assemble URLs:
//   1) "Generate Webhook Token" -> fills webhook_token with a random secret.
//   2) "Copy Webhook URLs" + a banner listing the four ready-to-register inbound URLs
//      https://<host>/webhooks/telephony/<provider>/<token>/<event> (one per Acefone trigger).
// Shown once a provider is set. The token both authenticates the caller and identifies the
// receiving account (telephony.routing.account_by_webhook_token). No fork — a Client Script.

const TELEPHONY_WEBHOOK_EVENTS = [
  'inbound_answered',
  'inbound_complete',
  'outbound_answered',
  'outbound_complete',
];

frappe.ui.form.on('CRM Telephony Account', {
  refresh(frm) {
    if (!frm.doc.provider) return;

    frm.add_custom_button(__('Generate Webhook Token'), () => {
      frm.set_value('webhook_token', telephony_random_token());
      frappe.show_alert({ message: __('Webhook token generated — Save to apply.'), indicator: 'green' });
    });

    if (frm.doc.webhook_token) {
      frm.add_custom_button(__('Copy Webhook URLs'), () => {
        frappe.utils.copy_to_clipboard(telephony_webhook_urls(frm).join('\n'));
      });
    }

    telephony_show_webhook_urls(frm);
  },

  // keep the banner in sync the moment the token changes (generate / manual edit)
  webhook_token(frm) {
    telephony_show_webhook_urls(frm);
  },
});

function telephony_random_token() {
  const bytes = new Uint8Array(30);
  window.crypto.getRandomValues(bytes);
  // url-safe-ish: base64 then strip the three chars that don't belong in a path segment
  return btoa(String.fromCharCode.apply(null, bytes)).replace(/[/+=]/g, '').slice(0, 40);
}

function telephony_webhook_urls(frm) {
  const provider = (frm.doc.provider || '').toLowerCase();
  const token = frm.doc.webhook_token || '';
  return TELEPHONY_WEBHOOK_EVENTS.map(
    (ev) => `${window.location.origin}/webhooks/telephony/${provider}/${token}/${ev}`
  );
}

function telephony_show_webhook_urls(frm) {
  if (!frm.doc.provider) return;
  frm.dashboard.clear_headline();
  if (!frm.doc.webhook_token) return;
  const rows = telephony_webhook_urls(frm)
    .map((u) => '<span style="font-family:monospace">' + frappe.utils.escape_html(u) + '</span>')
    .join('<br>');
  frm.dashboard.set_headline(
    __('Inbound webhook URLs (one per Acefone trigger)') + ':<br>' + rows + '<br>' +
      __('register these on the Acefone dashboard (use the Copy Webhook URLs button).')
  );
}
