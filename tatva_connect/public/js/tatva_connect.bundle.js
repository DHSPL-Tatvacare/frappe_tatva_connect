// Shared Desk helpers for the account + settings forms (WhatsApp Account, CRM Telephony Account, CRM Push Settings), loaded on every Desk page via hooks.py `app_include_js` as a content-hashed bundle.
// `tatva_enable_secret_reveal(frm, fieldnames)` — make the eye on a Password field reveal the real secret (a saved one holds only asterisks).
// `tatva_webhook_random_token()` — generate a URL-safe-ish secret for a fresh token.
// `tatva_render_webhook_urls(frm, opts)` — fetch the REAL URL(s) server-side and paint the banner; remembers them for the Copy button.
// `tatva_set_grid_row_options(grid, cdn, fieldname, data)` — feed ONE opened grid row's dropdown (per-row option sets).
// `tatva_set_grid_column_options(grid, fieldname, values)` — feed a grid column's dropdown for EVERY row (one option set).

// Per-row grid options; the control exists only once the row is OPEN, so callers use `form_render`.
window.tatva_set_grid_row_options = function tatva_set_grid_row_options(grid, cdn, fieldname, data) {
  const grid_row = grid && grid.grid_rows_by_docname && grid.grid_rows_by_docname[cdn];
  const field = grid_row && grid_row.grid_form && grid_row.grid_form.fields_dict
    ? grid_row.grid_form.fields_dict[fieldname]
    : null;
  if (!field) return;
  // Autocomplete takes {value,label} pairs; Select/Data fall back to a "\n" options string.
  if (typeof field.set_data === 'function') {
    field.set_data(data || []);
  } else {
    field.df.options = ['', ...(data || []).map((d) => d.value)].join('\n');
    field.refresh();
  }
}

// Column-wide options: the same set for every row, so it goes on the docfield rather than a row control.
window.tatva_set_grid_column_options = function tatva_set_grid_column_options(grid, fieldname, values) {
  if (!grid) return;
  try {
    grid.update_docfield_property(fieldname, 'options', ['', ...(values || [])].join('\n'));
  } catch (e) {
    // Field absent / grid not built yet — a safe no-op, exactly as the surfaces this replaces treated it.
  }
}

// A saved Password field holds only asterisks, so the eye must fetch the plaintext from the server to reveal anything.
window.tatva_enable_secret_reveal = function tatva_enable_secret_reveal(frm, fieldnames) {
  // The form script's refresh runs before the Password controls are made, so wait for the toggle to exist.
  (fieldnames || []).forEach((fieldname) => tatva_bind_secret_reveal(frm, fieldname, 0));
}

window.tatva_bind_secret_reveal = function tatva_bind_secret_reveal(frm, fieldname, attempt) {
  if (frm.is_new()) return;
  const ctrl = frm.get_field(fieldname);
  if (!ctrl || !ctrl.toggle_password) {
    if (attempt < 20) setTimeout(() => tatva_bind_secret_reveal(frm, fieldname, attempt + 1), 100);
    return;
  }

  // Frappe's Password control scores every keystroke against frappe.core...test_password_strength — on a
  // REVEALED field that would POST the provider's secret to a user-password endpoint, so the check is off.
  if (ctrl.disable_password_checks) ctrl.disable_password_checks();

  // Its keyup handler also re-hides the eye whenever the value contains a '*', which is every saved secret:
  // one keystroke and the toggle would vanish until reload. Keep it shown after the native handler runs.
  ctrl.toggle_password.removeClass('hidden');
  ctrl.$input.on('keyup', () => setTimeout(() => ctrl.toggle_password.removeClass('hidden'), 600));

  ctrl.toggle_password.off('click').on('click', () => {
    if (ctrl.$input.attr('type') === 'text') {
      ctrl.$input.val(ctrl.value || '').attr('type', 'password');
      ctrl.toggle_password.html(frappe.utils.icon('eye', 'sm'));
      return;
    }
    frappe.call({
      method: 'tatva_connect.api.account_secrets.reveal',
      args: { doctype: frm.doctype, name: frm.doc.name, fieldname },
      callback: (r) => {
        const secret = (r.message || {}).value;
        if (!secret) {
          frappe.show_alert({ message: __('Not set — type a value in the field.'), indicator: 'orange' });
          return;
        }
        ctrl.$input.val(secret).attr('type', 'text');
        ctrl.toggle_password.html(frappe.utils.icon('eye-off', 'sm'));
      },
    });
  });
}

// A pass/fail list from any server-side validate endpoint returning {ok, checks:[{label, passed, detail}]}.
// One renderer, so every "is this credential real" answer in the app reads the same way.
// Laid out as a grid: mark, label, value. A label and its value never run into each other.
window.tatva_show_check_report = function tatva_show_check_report(report, opts) {
  const rows = (report.checks || [])
    .map((c) => {
      const mark = c.passed
        ? '<span style="color:var(--green-600)">&#10003;</span>'
        : '<span style="color:var(--red-600)">&#10007;</span>';
      const label = frappe.utils.escape_html(c.label);
      const detail = c.detail
        ? '<span style="color:' + (c.passed ? 'var(--text-color)' : 'var(--red-600)') + '">' +
          frappe.utils.escape_html(c.detail) + '</span>'
        : '<span style="color:var(--text-muted)">' + (c.passed ? 'ok' : '&mdash;') + '</span>';
      return (
        '<div style="display:contents">' +
        '<div>' + mark + '</div>' +
        '<div style="color:var(--text-muted)">' + label + '</div>' +
        '<div>' + detail + '</div>' +
        '</div>'
      );
    })
    .join('');
  frappe.msgprint({
    title: report.ok ? __(opts.ok_title) : __(opts.fail_title),
    indicator: report.ok ? 'green' : 'red',
    message: rows
      ? '<div style="display:grid;grid-template-columns:auto max-content 1fr;gap:6px 12px;align-items:baseline">' +
        rows + '</div>'
      : __('Nothing to check — the form is blank.'),
  });
}

window.tatva_webhook_random_token = function tatva_webhook_random_token() {
  const bytes = new Uint8Array(30);
  window.crypto.getRandomValues(bytes);
  // url-safe-ish: base64 then strip the three chars that don't belong in a path segment
  return btoa(String.fromCharCode.apply(null, bytes)).replace(/[/+=]/g, '').slice(0, 40);
}

// opts: { single_label, multi_label, register_hint }
//   single_label — banner label when there's exactly one URL (WhatsApp)
//   multi_label  — banner label when there are several URLs (telephony events)
//   register_hint — trailing "register these on the <dashboard>…" sentence
window.tatva_render_webhook_urls = function tatva_render_webhook_urls(frm, opts) {
  frm.dashboard.clear_headline();
  frm.__tatva_webhook_urls = [];
  if (frm.is_new()) return;

  frappe.call({
    method: 'tatva_connect.webhooks.urls.get_account_webhook_urls',
    args: { account_doctype: frm.doctype, name: frm.doc.name },
    callback: (r) => {
      const res = r.message || {};
      const urls = res.urls || [];
      const targets = res.targets || [];
      frm.__tatva_webhook_urls = urls;
      if (!res.token_set || !targets.length) return;

      // Each URL is printed with the provider-dashboard config it belongs to — a bare list of URLs tells an operator nothing about where each one goes.
      const rows = targets
        .map((t) => {
          const cfg = frappe.utils.escape_html(t.register_as || '');
          const note = t.note ? ' <i>(' + frappe.utils.escape_html(t.note) + ')</i>' : '';
          const url = '<span style="font-family:monospace">' + frappe.utils.escape_html(t.url) + '</span>';
          return '<b>' + cfg + '</b>' + note + '<br>' + url;
        })
        .join('<br><br>');
      const label = targets.length > 1 ? opts.multi_label : opts.single_label;
      frm.dashboard.set_headline(__(label) + ':<br><br>' + rows + '<br><br>' + __(opts.register_hint));
    },
  });
}
