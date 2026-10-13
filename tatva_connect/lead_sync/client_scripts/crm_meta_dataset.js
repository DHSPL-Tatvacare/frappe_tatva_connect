// Desk Client Script — CRM Meta Dataset: the Graph versions Facebook Apps offer, the stage picker, Preview and the Send menu.

const TATVA_META_API = 'tatva_connect.lead_sync.api.';

frappe.ui.form.on('CRM Meta Dataset', {
  setup(frm) {
    // A dataset sends a lead from the stage it sits at, so a stage that only groups others is never offered.
    frm.set_query('crm_stage', 'stages', () => ({ filters: { selectable: 1 } }));
  },

  onload(frm) {
    // The versions live on CRM Facebook App's field, so they are read from its meta rather than typed twice.
    frappe.model.with_doctype('CRM Facebook App', () => {
      const df = frappe.meta.get_docfield('CRM Facebook App', 'graph_api_version');
      frm.set_df_property('graph_api_version', 'options', (df && df.options) || '');
    });
  },

  refresh(frm) {
    if (frm.is_new()) return;

    frm.add_custom_button(__('Preview'), () => tatva_meta_preview(frm));

    if (frm.doc.test_event_code) {
      frm.add_custom_button(__('Send Test Event'), () => tatva_meta_test_event(frm), __('Send'));
    }
    if (frm.doc.enabled && !(frm.doc.__onload || {}).has_sent) {
      frm.add_custom_button(__('Send Past Days...'), () => tatva_meta_past_days(frm), __('Send'));
    }
  },
});

function tatva_meta_counts(res) {
  const rows = Object.entries(res.events || {})
    .map(([name, count]) => `<tr><td>${frappe.utils.escape_html(name)}</td><td style="text-align:right">${count}</td></tr>`)
    .join('');
  const capped = res.capped ? '<p>' + __('More changes exist than one read takes, so these are the first ones.') + '</p>' : '';
  return rows
    ? `<table class="table table-bordered"><tbody>${rows}</tbody></table>${capped}`
    : __('No stage change in that time maps to an event.');
}

function tatva_meta_preview(frm) {
  frappe.call({
    method: TATVA_META_API + 'preview_conversions',
    args: { dataset: frm.doc.name },
    freeze: true,
    freeze_message: __('Reading stage changes…'),
    callback: (r) => {
      const res = r.message || {};
      frappe.msgprint({
        title: __('The last {0} days would send {1} events', [res.days, res.total || 0]),
        indicator: 'blue',
        message: tatva_meta_counts(res) + '<p>' + __('Nothing was sent.') + '</p>',
      });
    },
  });
}

function tatva_meta_test_event(frm) {
  frappe.confirm(__('Send one event for the newest Facebook lead to Test Events in Events Manager?'), () =>
    frappe.call({
      method: TATVA_META_API + 'send_test_event',
      args: { dataset: frm.doc.name },
      freeze: true,
      freeze_message: __('Sending to Meta…'),
      callback: (r) => tatva_show_check_report(r.message || {}, {
        ok_title: __('Meta received the test event. See Events Manager, Test Events.'),
        fail_title: __('Meta did not take the test event'),
      }),
    })
  );
}

function tatva_meta_past_days(frm) {
  frappe.prompt(
    { fieldname: 'days', fieldtype: 'Int', label: __('Days'), reqd: 1, default: 7, description: __('1 to 7. Meta refuses anything older.') },
    ({ days }) => frappe.call({
      method: TATVA_META_API + 'preview_conversions',
      args: { dataset: frm.doc.name, days },
      callback: (r) => {
        const res = r.message || {};
        frappe.confirm(
          __('Send the last {0} days, {1} events, on the next run? This is only possible before the first run.', [res.days, res.total || 0]),
          () => frappe.call({
            method: TATVA_META_API + 'send_past_days',
            args: { dataset: frm.doc.name, days },
            callback: () => {
              frappe.msgprint({
                // "Queued", not "sent": the scheduled run does the sending.
                title: __('Queued'),
                indicator: 'blue',
                message: __('The next {0} run sends them. Meta Sends lists each batch.', [frm.doc.send_frequency]),
              });
              frm.reload_doc();
            },
          })
        );
      },
    }),
    __('Send past days'),
    __('Continue')
  );
}
