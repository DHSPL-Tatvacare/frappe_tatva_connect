// Desk Client Script — CRM Telephony Agent: pick an account, fetch its extensions, take one; the rep's seat on that account is saved with its follow-me number.

frappe.ui.form.on('CRM Telephony Agent', {
  refresh(frm) {
    if (frm.is_new()) return;
    frm.add_custom_button(__('Fetch Extensions'), () => telephony_fetch_extensions(frm));
  },
});

function telephony_fetch_extensions(frm) {
  tatva_telephony_fetch({
    title: __('Fetch Extensions'),
    intro: __('Pick the account this rep calls through. The extension you pick is saved as their seat on that account: calls from leads routed to it ring that extension, and calls on its numbers are credited by it. Fetch again with another account to add a seat there.'),
    record: frm.doc,
    method: 'tatva_connect.telephony.discovery.extensions_for_agent',
    args: { agent: frm.doc.name },
    on_answer: (data) => telephony_pick_extension(frm, data),
  });
}

function telephony_pick_extension(frm, data) {
  tatva_pick_rows({
    title: __('Extensions on {0}', [data.account]),
    note: __('Tick this rep\'s extension. It is saved to the rep at once, with its follow-me number.'),
    refused: data.refused,
    action_label: __('Save to this rep'),
    single: true,
    columns: [
      { fieldname: 'name', label: __('Extension'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 2 },
      { fieldname: 'agent_name', label: __('Name on the provider'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 2 },
      { fieldname: 'group', label: __('Departments'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 2 },
      { fieldname: 'follow_me_number', label: __('Follow-me'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 2 },
      { fieldname: 'presence', label: __('On the provider'), fieldtype: 'HTML', in_list_view: 1, read_only: 1, columns: 1 },
      { fieldname: 'status', label: __('Status'), fieldtype: 'Data', in_list_view: 1, read_only: 1, columns: 1 },
    ],
    rows: (data.rows || []).map((r) => ({
      ...r,
      presence: tatva_status_pill(r.agent_status, { Available: 'green', Busy: 'orange', Offline: 'gray', Blocked: 'red', Disabled: 'red' }),
      status: r.taken_by === frm.doc.name ? __('This rep') : r.taken ? __('With {0}', [r.taken_by]) : '',
    })),
    on_pick: ([picked]) => telephony_take_extension(frm, data.account, picked),
  });
}

// The rep's open seat on this account is refreshed when it is the same extension, and closed now when it is replaced.
function telephony_take_extension(frm, account, picked) {
  const open = (frm.doc.telephony_extensions || []).filter((r) => r.telephony_account === account && !r.valid_to);
  const same = open.find((r) => r.extension === picked.name);
  const now = frappe.datetime.now_datetime();
  const changes = same
    ? [frappe.model.set_value(same.doctype, same.name, 'follow_me_number', picked.follow_me_number)]
    : open.map((r) => frappe.model.set_value(r.doctype, r.name, 'valid_to', now));
  if (!same) {
    frm.add_child('telephony_extensions', {
      telephony_account: account, extension: picked.name, follow_me_number: picked.follow_me_number, valid_from: open.length ? now : null,
    });
  }
  Promise.all(changes).then(() => frm.save());
}
