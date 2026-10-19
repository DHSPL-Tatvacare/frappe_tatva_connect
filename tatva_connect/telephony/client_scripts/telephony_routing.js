// Desk Client Script — CRM Telephony Routing: pick an account, fetch its numbers, tick the ones this product line answers on and calls from; the rule saves with them.

frappe.ui.form.on('CRM Telephony Routing', {
  refresh(frm) {
    if (frm.is_new() || !frm.doc.telephony_account) return;
    frm.add_custom_button(__('Fetch Numbers'), () => telephony_fetch_numbers(frm));
  },
});

// The rule's own account is offered first; a number from another account keeps that account as its override.
function telephony_fetch_numbers(frm) {
  tatva_telephony_fetch({
    title: __('Fetch Numbers'),
    intro: __('Pick the account the numbers belong to. The numbers you tick are saved to this rule: calls on them belong to its leads, and its reps can show them as caller ID.'),
    record: frm.doc,
    account: frm.doc.telephony_account,
    method: 'tatva_connect.telephony.discovery.numbers_for_rule',
    args: { routing: frm.doc.name },
    on_answer: (data) => telephony_pick_numbers(frm, data),
  });
}

function telephony_pick_numbers(frm, data) {
  tatva_pick_rows({
    title: __('Numbers on {0}', [data.account]),
    note: __('Tick the numbers this product line answers on and calls out from. They are saved to the rule at once.'),
    refused: data.refused,
    action_label: __('Save to this rule'),
    columns: [
      { fieldname: 'name', label: __('Number'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 3 },
      { fieldname: 'group', label: __('Rings'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 5 },
      { fieldname: 'status', label: __('Status'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 2 },
    ],
    rows: (data.rows || []).map((r) => ({
      name: r.name,
      group: r.group || __('Nothing (outgoing only)'),
      status: r.taken ? (r.taken_by === frm.doc.name ? __('Already listed') : __('On {0}', [r.taken_by])) : '',
      taken: r.taken,
      department: r.group,
    })),
    on_pick: (picked) => {
      const override = data.account !== frm.doc.telephony_account ? data.account : null;
      picked.forEach((r) => frm.add_child('dids', { did_number: r.name, label: r.department, enabled: 1, telephony_account: override }));
      frm.save();
    },
  });
}
