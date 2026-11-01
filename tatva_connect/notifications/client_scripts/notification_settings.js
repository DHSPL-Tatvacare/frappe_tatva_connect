// Desk Client Script — Notification Settings: push and bell types render as frappe's own email-types grid (its MultiCheck, 4 columns).

const TATVA_TYPE_LISTS = [
  // Push offers every type frappe can email; the bell only the notices tatva_connect writes, as the CRM's Bell screen does.
  { field: 'push_notification_types', types: 'frappe.desk.doctype.notification_settings.notification_settings.get_emailable_notification_types' },
  { field: 'bell_notification_types', types: 'tatva_connect.notifications.api.get_bell_types' },
];

frappe.ui.form.on('Notification Settings', {
  refresh(frm) {
    TATVA_TYPE_LISTS.forEach((list) => tatvaTypeChecklist(frm, list));
  },
});

function tatvaTypeChecklist(frm, { field, types }) {
  const wrapper = frm.fields_dict[`${field}_html`]?.wrapper;
  if (!wrapper) return;
  frm.tatva_type_lists = frm.tatva_type_lists || {};
  const selected = () => (frm.doc[field] || []).map((row) => row.notification_type);
  const control = frm.tatva_type_lists[field];
  if (control) {
    control.selected_options = selected();
    control.refresh_input();
    return;
  }
  $(wrapper).empty();
  frm.tatva_type_lists[field] = frappe.ui.form.make_control({
    parent: wrapper,
    render_input: true,
    df: {
      fieldname: field,
      fieldtype: 'MultiCheck',
      select_all: true,
      columns: 4,
      get_data: () =>
        frappe.xcall(types).then((names) => names.map((t) => ({ label: __(t), value: t, checked: selected().includes(t) }))),
      on_change: () => {
        const checked = frm.tatva_type_lists[field].get_checked_options();
        const child = frappe.meta.get_docfield(frm.doctype, field).options;
        (frm.doc[field] || [])
          .filter((row) => !checked.includes(row.notification_type))
          .forEach((row) => frappe.model.clear_doc(row.doctype, row.name));
        checked
          .filter((type) => !selected().includes(type))
          .forEach((type) => {
            frappe.model.add_child(frm.doc, child, field).notification_type = type;
          });
        frm.dirty();
      },
    },
  });
}
