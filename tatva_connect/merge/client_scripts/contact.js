// Desk Client Script — Contact: Merge folds a duplicate person into the one kept, and a save that would make a second one is stopped.

frappe.ui.form.on('Contact', {
  refresh: (frm) => tatva_merge_button(frm),
  // Asked once per save: does another contact already hold one of these phones or emails. A match stops the save.
  async validate(frm) {
    const phones = (frm.doc.phone_nos || []).map((row) => row.phone).filter(Boolean);
    const emails = (frm.doc.email_ids || []).map((row) => row.email_id).filter(Boolean);
    if (!phones.length && !emails.length) return;
    const { message: holder } = await frappe.call({
      method: 'tatva_connect.helpdesk.contact.existing_holder',
      args: { phones, emails, exclude: frm.is_new() ? null : frm.doc.name },
    });
    if (!holder) return;
    frappe.validated = false;
    const link = `<a href="/app/contact/${encodeURIComponent(holder)}">${frappe.utils.escape_html(holder)}</a>`;
    frappe.msgprint({ title: __('Contact already exists'), indicator: 'orange',
      message: __('{0} already has this phone or email. Open that contact, or use Merge.', [link]) });
  },
});
