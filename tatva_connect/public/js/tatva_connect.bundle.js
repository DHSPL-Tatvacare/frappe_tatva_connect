// Shared Desk helpers for the webhook account forms (WhatsApp Account, CRM Telephony
// Account). Loaded on every Desk page via hooks.py `app_include_js`. Both forms' Client
// Scripts call these instead of carrying their own copy.
//
// `tatva_webhook_random_token()` — generate a URL-safe-ish secret for a fresh token.
// `tatva_render_webhook_urls(frm, opts)` — fetch the REAL URL(s) from the server (the
//   token is a Password field, masked as `***` after save, so the URL can NOT be built
//   from frm.doc.<token>) and paint the headline banner; remembers the URLs on the form
//   so the Copy button can read them back. Re-fetches on each refresh / token change.

// A saved Password field holds only asterisks, so the eye must fetch the plaintext from the server to reveal anything.
window.tatva_enable_secret_reveal = function tatva_enable_secret_reveal(frm, fieldnames) {
  // The form script's refresh runs before the Password controls are made, so wait for the toggle to exist.
  (fieldnames || []).forEach((fieldname) => tatva_bind_secret_reveal(frm, fieldname, 0));
}

window.tatva_bind_secret_reveal = function tatva_bind_secret_reveal(frm, fieldname, attempt) {
  if (frm.is_new()) return;
  const ctrl = frm.get_field(fieldname);
  if (!ctrl || !ctrl.toggle_password) {
    if (attempt < 20) setTimeout(() => tatva_bind_secret_reveal(frm, fieldname, attempt + 1), 100);
    return;
  }

  ctrl.toggle_password.removeClass('hidden');
  ctrl.toggle_password.off('click').on('click', () => {
    if (ctrl.$input.attr('type') === 'text') {
      ctrl.$input.val(ctrl.value || '').attr('type', 'password');
      ctrl.toggle_password.html(frappe.utils.icon('eye', 'sm'));
      return;
    }
    frappe.call({
      method: 'tatva_connect.api.account_secrets.reveal',
      args: { doctype: frm.doctype, name: frm.doc.name, fieldname },
      callback: (r) => {
        const secret = (r.message || {}).value;
        if (!secret) {
          frappe.show_alert({ message: __('Not set — type a value in the field.'), indicator: 'orange' });
          return;
        }
        ctrl.$input.val(secret).attr('type', 'text');
        ctrl.toggle_password.html(frappe.utils.icon('eye-off', 'sm'));
      },
    });
  });
}

window.tatva_webhook_random_token = function tatva_webhook_random_token() {
  const bytes = new Uint8Array(30);
  window.crypto.getRandomValues(bytes);
  // url-safe-ish: base64 then strip the three chars that don't belong in a path segment
  return btoa(String.fromCharCode.apply(null, bytes)).replace(/[/+=]/g, '').slice(0, 40);
}

// opts: { single_label, multi_label, register_hint }
//   single_label — banner label when there's exactly one URL (WhatsApp)
//   multi_label  — banner label when there are several URLs (telephony events)
//   register_hint — trailing "register these on the <dashboard>…" sentence
window.tatva_render_webhook_urls = function tatva_render_webhook_urls(frm, opts) {
  frm.dashboard.clear_headline();
  frm.__tatva_webhook_urls = [];
  if (frm.is_new()) return;

  frappe.call({
    method: 'tatva_connect.webhooks.urls.get_account_webhook_urls',
    args: { account_doctype: frm.doctype, name: frm.doc.name },
    callback: (r) => {
      const res = r.message || {};
      const urls = res.urls || [];
      const targets = res.targets || [];
      frm.__tatva_webhook_urls = urls;
      if (!res.token_set || !targets.length) return;

      // Each URL is printed with the provider-dashboard config it belongs to — a bare list of URLs tells an operator nothing about where each one goes.
      const rows = targets
        .map((t) => {
          const cfg = frappe.utils.escape_html(t.register_as || '');
          const note = t.note ? ' <i>(' + frappe.utils.escape_html(t.note) + ')</i>' : '';
          const url = '<span style="font-family:monospace">' + frappe.utils.escape_html(t.url) + '</span>';
          return '<b>' + cfg + '</b>' + note + '<br>' + url;
        })
        .join('<br><br>');
      const label = targets.length > 1 ? opts.multi_label : opts.single_label;
      frm.dashboard.set_headline(__(label) + ':<br><br>' + rows + '<br><br>' + __(opts.register_hint));
    },
  });
}
