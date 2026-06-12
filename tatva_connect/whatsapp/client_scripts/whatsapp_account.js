// Desk Client Script — WhatsApp Account form (Frappe Desk, /app/whatsapp-account).
// WATI helper affordances so an operator never has to hand-craft a secret or assemble a URL:
//   1) "Generate Webhook Token" button  -> fills custom_webhook_token with a random secret.
//   2) "Copy Webhook URL" button + a headline banner showing the ready-to-register inbound
//      URL  https://<host>/webhooks/whatsapp/wati/<token>.
// WATI-only: everything is gated on custom_is_wati, so non-WATI accounts see nothing extra.
// This is the Desk-form counterpart to the CRM SPA's CRM Form Scripts (see form_scripts_seed).
// No fork — a Client Script override on an upstream doctype.

frappe.ui.form.on('WhatsApp Account', {
  refresh(frm) {
    if (!frm.doc.custom_is_wati) return;

    frm.add_custom_button(__('Generate Webhook Token'), () => {
      frm.set_value('custom_webhook_token', wati_random_token());
      frappe.show_alert({ message: __('Webhook token generated — Save to apply.'), indicator: 'green' });
    });

    if (frm.doc.custom_webhook_token) {
      frm.add_custom_button(__('Copy Webhook URL'), () => {
        frappe.utils.copy_to_clipboard(wati_webhook_url(frm));
      });
    }

    wati_show_webhook_url(frm);
  },

  // keep the banner in sync the moment the token changes (generate / manual edit)
  custom_webhook_token(frm) {
    wati_show_webhook_url(frm);
  },
});

function wati_random_token() {
  const bytes = new Uint8Array(30);
  window.crypto.getRandomValues(bytes);
  // url-safe-ish: base64 then strip the three chars that don't belong in a path segment
  return btoa(String.fromCharCode.apply(null, bytes)).replace(/[/+=]/g, '').slice(0, 40);
}

function wati_webhook_url(frm) {
  return `${window.location.origin}/webhooks/whatsapp/wati/${frm.doc.custom_webhook_token || ''}`;
}

function wati_show_webhook_url(frm) {
  if (!frm.doc.custom_is_wati) return;
  frm.dashboard.clear_headline();
  if (!frm.doc.custom_webhook_token) return;
  frm.dashboard.set_headline(
    __('Inbound webhook URL') +
      ': <code>' + frappe.utils.escape_html(wati_webhook_url(frm)) + '</code> — ' +
      __('register this on the WATI dashboard (use the Copy Webhook URL button).')
  );
}
