// Desk Client Script — Lead Sync Source: Sync Now says what it will fetch, and the app is one click away.

frappe.ui.form.on('Lead Sync Source', {
  refresh(frm) {
    if (frm.is_new()) return;

    tatva_validate_token(frm, 'Source will carry a crawl', 'Source will not carry a crawl');

    // add_custom_button keeps the first handler for a label, so the fork's button is removed to replace it.
    frm.remove_custom_button(__('Sync Now'));
    frm.add_custom_button(__('Sync Now'), () => {
      frappe.confirm(tatva_sync_now_prompt(frm.doc), () =>
        frm.call('sync_leads').then(() => {
          frappe.msgprint({
            // "Queued", not "done": the work runs on the long queue and this fires as soon as it is accepted.
            title: __('Sync queued'),
            indicator: 'blue',
            message: __('The crawl runs in the background. Last Synced At moves when it finishes, and anything refused appears under Lead Sync Failures.'),
          });
        })
      );
    });

    if (frm.doc.type === 'Facebook' && frm.doc.facebook_app) {
      frm.add_custom_button(__('Open Facebook App'), () =>
        frappe.set_route('Form', 'CRM Facebook App', frm.doc.facebook_app)
      );
    }
  },
});
