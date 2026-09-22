// Desk Client Script — CRM Telephony Agent: Acefone reports every extension and the departments it answers for; an extension another rep holds is shown as theirs.

frappe.ui.form.on('CRM Telephony Agent', {
  refresh(frm) {
    if (frm.is_new()) return;
    frm.add_custom_button(__('Fetch Extensions from Acefone'), () => {
      frappe.dom.freeze(__('Asking Acefone…'));
      frappe
        .call({
          method: 'tatva_connect.telephony.discovery.extensions_for_agent',
          args: { agent: frm.doc.name },
        })
        .always(() => frappe.dom.unfreeze())
        .then((r) => telephony_pick_extensions(frm, (r && r.message) || {}));
    });
  },
});

function telephony_pick_extensions(frm, data) {
  tatva_pick_rows({
    title: __('Extensions on Acefone'),
    note: __('One rep holds one extension per account.'),
    refused: data.refused,
    headers: [__('Extension'), __('Name on Acefone'), __('Departments')],
    action_label: __('Add to this rep'),
    rows: (data.rows || []).map((r) => ({
      group: r.group,
      taken: r.taken,
      taken_label: __('With {0}', [r.taken_by]),
      cells: [r.name, r.agent_name, r.departments || __('None')],
      row: r,
    })),
    on_pick: (picked) => {
      picked.forEach(({ row }) =>
        frm.add_child('telephony_extensions', { telephony_account: row.group, extension: row.name }),
      );
      frm.refresh_field('telephony_extensions');
      frappe.show_alert({ message: __('{0} added — Save to apply.', [picked.length]), indicator: 'green' });
    },
  });
}
