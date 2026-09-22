// Desk Client Script — CRM Telephony Routing: Acefone reports this account's numbers by department; a department is ticked whole, and a number another rule owns is shown as theirs.

frappe.ui.form.on('CRM Telephony Routing', {
  refresh(frm) {
    if (frm.is_new() || !frm.doc.telephony_account) return;
    frm.add_custom_button(__('Fetch Numbers from Acefone'), () => {
      frappe.dom.freeze(__('Asking Acefone…'));
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
    note: __('Tick a department to take all of its numbers. This grain then answers on them, and calls go out from them.'),
    refused: data.refused,
    headers: [__('Number')],
    group_pick: true,
    action_label: __('Add to this rule'),
    rows: (data.rows || []).map((r) => ({
      group: r.group || __('Not routed to a department'),
      taken: r.taken,
      taken_label: r.taken_by === frm.doc.name ? __('Already listed') : __('On {0}', [r.taken_by]),
      cells: [r.name],
      row: r,
    })),
    on_pick: (picked) => {
      picked.forEach(({ row }) =>
        frm.add_child('dids', { did_number: row.name, label: row.group, enabled: 1 }),
      );
      frm.refresh_field('dids');
      frappe.show_alert({ message: __('{0} added — Save to apply.', [picked.length]), indicator: 'green' });
    },
  });
}
