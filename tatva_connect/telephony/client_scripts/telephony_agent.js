// Desk Client Script — CRM Telephony Agent: the provider reports every extension and the departments it answers for; the ticked ones join this rep's Extensions table for the operator to save.

frappe.ui.form.on('CRM Telephony Agent', {
  refresh(frm) {
    if (frm.is_new()) return;
    frm.add_custom_button(__('Fetch Extensions'), () => {
      frappe.dom.freeze(__('Asking the provider…'));
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
    title: __('Extensions on the provider'),
    note: __('One rep holds one extension per account.'),
    refused: data.refused,
    action_label: __('Add to this rep'),
    columns: [
      { fieldname: 'name', label: __('Extension'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 2 },
      { fieldname: 'group', label: __('Account'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 2 },
      { fieldname: 'agent_name', label: __('Name on the provider'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 2 },
      { fieldname: 'departments', label: __('Departments'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 2 },
      { fieldname: 'presence', label: __('On the provider'), fieldtype: 'HTML', in_list_view: 1, read_only: 1, columns: 2 },
      { fieldname: 'status', label: __('Status'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 2 },
    ],
    rows: (data.rows || []).map((r) => ({
      name: r.name,
      group: r.group,
      agent_name: r.agent_name,
      departments: r.departments,
      presence: tatva_status_pill(r.agent_status, { Available: 'green', Busy: 'orange', Offline: 'gray', Blocked: 'red', Disabled: 'red' }),
      status: r.taken ? __('With {0}', [r.taken_by]) : '',
      taken: r.taken,
    })),
    on_pick: (picked) => {
      picked.forEach((r) => frm.add_child('telephony_extensions', { telephony_account: r.group, extension: r.name }));
      frm.refresh_field('telephony_extensions');
      frappe.show_alert({ message: __('{0} added — Save to apply.', [picked.length]), indicator: 'green' });
    },
  });
}
