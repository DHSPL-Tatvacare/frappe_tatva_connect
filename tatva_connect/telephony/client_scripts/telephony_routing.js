// Desk Client Script — CRM Telephony Routing: the provider reports this account's numbers and the department each reaches; the ticked ones join this rule's Numbers table for the operator to save.

frappe.ui.form.on('CRM Telephony Routing', {
  refresh(frm) {
    if (frm.is_new() || !frm.doc.telephony_account) return;
    frm.add_custom_button(__('Fetch Numbers'), () => {
      frappe.dom.freeze(__('Asking the provider…'));
      frappe
        .call({
          method: 'tatva_connect.telephony.discovery.numbers_for_rule',
          args: { routing: frm.doc.name },
        })
        .always(() => frappe.dom.unfreeze())
        .then((r) => telephony_pick_numbers(frm, (r && r.message) || {}));
    });
  },
});

function telephony_pick_numbers(frm, data) {
  tatva_pick_rows({
    title: __('Numbers on {0}', [data.account]),
    note: __('Tick the numbers this product line should answer on and call out from.'),
    refused: data.refused,
    action_label: __('Add to this rule'),
    columns: [
      { fieldname: 'name', label: __('Number'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 3 },
      { fieldname: 'group', label: __('Department'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 5 },
      { fieldname: 'status', label: __('Status'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 2 },
    ],
    rows: (data.rows || []).map((r) => ({
      name: r.name,
      group: r.group || __('Not routed'),
      status: r.taken ? (r.taken_by === frm.doc.name ? __('Already listed') : __('On {0}', [r.taken_by])) : '',
      taken: r.taken,
      department: r.group,
    })),
    on_pick: (picked) => {
      picked.forEach((r) => frm.add_child('dids', { did_number: r.name, label: r.department, enabled: 1 }));
      frm.refresh_field('dids');
      frappe.show_alert({ message: __('{0} added — Save to apply.', [picked.length]), indicator: 'green' });
    },
  });
}
