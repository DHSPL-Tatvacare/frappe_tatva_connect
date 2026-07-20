// Desk Client Script — Lead Sync Source form (/app/lead-sync-source).
// Two affordances, both answering questions an operator otherwise has to guess at:
//   1) "Validate Token" — asks Graph whether the stored credential would carry a crawl, and reports
//      kind, app, expiry and each required scope. A verdict is returned; the token never is.
//   2) "Refresh From Facebook" — re-runs discovery, so a new Page token, a new form and an edited
//      form's questions all become visible without deleting and recreating the source.
// The token is a Password field (asterisks once saved), so the eye toggle reads the real value back
// through the shared helper (public/js/tatva_connect.bundle.js).
// No fork — a Client Script override on an upstream doctype.

frappe.ui.form.on('Lead Sync Source', {
  refresh(frm) {
    tatva_enable_secret_reveal(frm, ['access_token']);

    if (frm.is_new() || frm.doc.type !== 'Facebook') return;

    frm.add_custom_button(__('Validate Token'), () => {
      frappe.call({
        method: 'tatva_connect.lead_sync.api.validate_token',
        args: { doctype: frm.doctype, name: frm.doc.name },
        freeze: true,
        freeze_message: __('Asking Facebook…'),
        callback: (r) =>
          tatva_show_check_report(r.message || {}, {
            ok_title: 'Token will carry a crawl',
            fail_title: 'Token will not carry a crawl',
          }),
      });
    });

    frm.add_custom_button(__('Refresh From Facebook'), () => {
      frappe.call({
        method: 'tatva_connect.lead_sync.api.refresh_from_facebook',
        args: { name: frm.doc.name },
        freeze: true,
        freeze_message: __('Refreshing Pages and forms…'),
        callback: (r) => {
          const res = r.message || {};
          frappe.msgprint({
            title: __('Refreshed from Facebook'),
            indicator: 'green',
            message: __('{0} Page(s) and {1} form(s) are now current.', [res.pages || 0, res.forms || 0]),
          });
        },
      });
    });
  },
});
