// Desk Client Script — Notification Settings: push types render as frappe's own email-types grid (its MultiCheck, its type list, 4 columns).

const TATVA_PUSH_TYPES = 'push_notification_types';

frappe.ui.form.on('Notification Settings', {
  refresh(frm) {
    const wrapper = frm.fields_dict.push_notification_types_html?.wrapper;
    if (!wrapper) return;
    const selected = () => (frm.doc[TATVA_PUSH_TYPES] || []).map((row) => row.notification_type);
    if (frm.tatva_push_types) {
      frm.tatva_push_types.selected_options = selected();
      frm.tatva_push_types.refresh_input();
      return;
    }
    $(wrapper).empty();
    frm.tatva_push_types = frappe.ui.form.make_control({
      parent: wrapper,
      render_input: true,
      df: {
        fieldname: TATVA_PUSH_TYPES,
        fieldtype: 'MultiCheck',
        select_all: true,
        columns: 4,
        get_data: () =>
          frappe
            .xcall('frappe.desk.doctype.notification_settings.notification_settings.get_emailable_notification_types')
            .then((types) => types.map((t) => ({ label: __(t), value: t, checked: selected().includes(t) }))),
        on_change: () => {
          const checked = frm.tatva_push_types.get_checked_options();
          const child = frappe.meta.get_docfield(frm.doctype, TATVA_PUSH_TYPES).options;
          (frm.doc[TATVA_PUSH_TYPES] || [])
            .filter((row) => !checked.includes(row.notification_type))
            .forEach((row) => frappe.model.clear_doc(row.doctype, row.name));
          checked
            .filter((type) => !selected().includes(type))
            .forEach((type) => {
              frappe.model.add_child(frm.doc, child, TATVA_PUSH_TYPES).notification_type = type;
            });
          frm.dirty();
        },
      },
    });
  },
});
