// CRM Telephony Account form: the shared Webhook menu + URL banner (tatva_webhook_actions), and the provider's own actions in one menu named for it, shown once a provider is set.

frappe.ui.form.on('CRM Telephony Account', {
  refresh(frm) {
    if (!frm.doc.provider) return;

    tatva_webhook_actions(frm, 'webhook_token', TELEPHONY_WEBHOOK_BANNER);
    if (!frm.is_new()) {
      const group = __(frm.doc.provider);
      tatva_validate_token(frm, 'Account is ready', 'Account is not ready', group, {
        method: 'tatva_connect.telephony.discovery.validate_token', freeze_message: __('Asking the provider…'),
      });
      frm.add_custom_button(__('Fetch Dispositions'), () => telephony_fetch_dispositions(frm), group);
    }
  },

  onload_post_render(frm) {
    if (frm.__telephony_bound) return;
    frm.__telephony_bound = true;
    frappe.realtime.on('telephony_dispositions_fetched', (report) => {
      if (report.account !== frm.doc.name) return;
      tatva_show_check_report(report, {
        ok_title: __('Dispositions fetched for {0}', [frappe.datetime.str_to_user(report.day)]),
        fail_title: __('Dispositions not fully fetched for {0}', [frappe.datetime.str_to_user(report.day)]),
      });
    });
  },

  // re-fetch the banner the moment the token changes (generate / manual edit)
  webhook_token(frm) {
    telephony_show_webhook_urls(frm);
  },
});

const TELEPHONY_WEBHOOK_BANNER = {
  single_label: 'Register this webhook on the Acefone dashboard',
  multi_label: 'Register these webhooks on the Acefone dashboard',
  register_hint: 'Acefone → Webhooks → Add: Channel Voice, Content Type JSON, keep the default payload template, Retry webhook ON. Outbound answered has no Acefone trigger and is not listed.',
};

function telephony_show_webhook_urls(frm) {
  if (!frm.doc.provider) return;
  tatva_render_webhook_urls(frm, TELEPHONY_WEBHOOK_BANNER);
}

// One day's dispositions onto the calls this account already logged; the job runs on a worker and the result arrives here.
function telephony_fetch_dispositions(frm) {
  frappe.prompt(
    [{ fieldname: 'day', label: __('Day'), fieldtype: 'Date', reqd: 1, default: frappe.datetime.get_today() }],
    ({ day }) => frappe.call({
      method: 'tatva_connect.telephony.reconcile.fetch_dispositions',
      args: { account: frm.doc.name, day },
      callback: () => frappe.show_alert({ message: __('Fetching dispositions. The result opens here when it is done.'), indicator: 'blue' }),
    }),
    __('Fetch Dispositions'),
    __('Fetch'),
  );
}
