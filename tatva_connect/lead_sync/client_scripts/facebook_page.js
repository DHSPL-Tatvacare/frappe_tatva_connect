// Desk Client Script — Facebook Page form (/app/facebook-page).
// The Page token is what the crawl actually runs on, and once derived from a long-lived user token it
// does not expire. "Validate Token" is where that is confirmed rather than assumed: a Page token
// reporting an expiry means the user token it came from was short-lived when discovery ran.
// No fork — a Client Script override on an upstream doctype.

frappe.ui.form.on('Facebook Page', {
  refresh(frm) {
    if (frm.is_new()) return;

    frm.add_custom_button(__('Validate Token'), () => {
      frappe.call({
        method: 'tatva_connect.lead_sync.api.validate_token',
        args: { doctype: frm.doctype, name: frm.doc.name },
        freeze: true,
        freeze_message: __('Asking Facebook…'),
        callback: (r) =>
          tatva_show_check_report(r.message || {}, {
            ok_title: 'Page token will carry a crawl',
            fail_title: 'Page token will not carry a crawl',
          }),
      });
    });
  },
});
