// Desk Client Script — Facebook Page form (/app/facebook-page).
// The Page token is what the crawl actually runs on, and once derived from a long-lived user token it
// does not expire. "Validate Token" is where that is confirmed rather than assumed: a Page token
// reporting an expiry means the user token it came from was short-lived when discovery ran.
// No fork — a Client Script override on an upstream doctype.

frappe.ui.form.on('Facebook Page', {
  refresh(frm) {
    if (frm.is_new()) return;

    tatva_validate_token(frm, 'Page token will carry a crawl', 'Page token will not carry a crawl');
  },
});
