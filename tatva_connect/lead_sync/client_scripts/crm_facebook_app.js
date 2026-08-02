// Desk Client Script — CRM Facebook App (/app/crm-facebook-app).
// The App Secret is a Password field, which holds only asterisks once saved, so the stock eye toggle
// reveals nothing; the shared helper (public/js/tatva_connect.bundle.js) reads the real value back.
// It is what a pasted short-lived token is exchanged against, so an operator confirming it is set
// correctly is the difference between a crawl that survives the hour and one that does not.

frappe.ui.form.on('CRM Facebook App', {
  refresh(frm) {
    tatva_enable_secret_reveal(frm, ['app_secret']);
  },
});
