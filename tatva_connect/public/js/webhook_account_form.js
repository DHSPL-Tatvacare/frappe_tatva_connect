// Shared Desk helpers for the webhook account forms (WhatsApp Account, CRM Telephony
// Account). Loaded on every Desk page via hooks.py `app_include_js`. Both forms' Client
// Scripts call these instead of carrying their own copy.
//
// `tatva_webhook_random_token()` — generate a URL-safe-ish secret for a fresh token.
// `tatva_render_webhook_urls(frm, opts)` — fetch the REAL URL(s) from the server (the
//   token is a Password field, masked as `***` after save, so the URL can NOT be built
//   from frm.doc.<token>) and paint the headline banner; remembers the URLs on the form
//   so the Copy button can read them back. Re-fetches on each refresh / token change.

function tatva_webhook_random_token() {
  const bytes = new Uint8Array(30);
  window.crypto.getRandomValues(bytes);
  // url-safe-ish: base64 then strip the three chars that don't belong in a path segment
  return btoa(String.fromCharCode.apply(null, bytes)).replace(/[/+=]/g, '').slice(0, 40);
}

// opts: { single_label, multi_label, register_hint }
//   single_label — banner label when there's exactly one URL (WhatsApp)
//   multi_label  — banner label when there are several URLs (telephony events)
//   register_hint — trailing "register these on the <dashboard>…" sentence
function tatva_render_webhook_urls(frm, opts) {
  frm.dashboard.clear_headline();
  frm.__tatva_webhook_urls = [];
  if (frm.is_new()) return;

  frappe.call({
    method: 'tatva_connect.webhooks.urls.get_account_webhook_urls',
    args: { account_doctype: frm.doctype, name: frm.doc.name },
    callback: (r) => {
      const res = r.message || {};
      const urls = res.urls || [];
      frm.__tatva_webhook_urls = urls;
      if (!res.token_set || !urls.length) return;

      const rows = urls
        .map((u) => '<span style="font-family:monospace">' + frappe.utils.escape_html(u) + '</span>')
        .join('<br>');
      const label = urls.length > 1 ? opts.multi_label : opts.single_label;
      frm.dashboard.set_headline(__(label) + ':<br>' + rows + '<br>' + __(opts.register_hint));
    },
  });
}
