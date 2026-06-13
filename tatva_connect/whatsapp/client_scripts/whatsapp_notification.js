// Desk Client Script — WhatsApp Notification form. Guides the one-time variable mapping:
// when a template is picked, show its WATI variables (name + sample) so the operator knows
// exactly what to map in the Fields table. Resolution needs this explicit mapping — no magic.
frappe.ui.form.on('WhatsApp Notification', {
  template(frm) { wati_notif_show_vars(frm); },
  refresh(frm) { wati_notif_show_vars(frm); },
});

function wati_notif_show_vars(frm) {
  frm.dashboard.clear_headline();
  if (!frm.doc.template) return;
  frappe.call({
    method: 'tatva_connect.api.whatsapp.get_template_variables',
    args: { template: frm.doc.template },
    callback: (r) => {
      const vars = (r.message && r.message.variables) || [];
      if (!vars.length) {
        frm.dashboard.set_headline(__('This template has no variables — no mapping needed.'));
        return;
      }
      const list = vars.map((v) => {
        const nm = (v.name && v.name !== String(v.index)) ? v.name : __('Variable') + ' ' + v.index;
        const label = frappe.utils.escape_html(nm);
        return v.hint ? `${label} (${__('e.g.')} ${frappe.utils.escape_html(v.hint)})` : label;
      }).join(', ');
      frm.dashboard.set_headline(__('Map these variables in the Fields table') + ': ' + list);
    },
  });
}
