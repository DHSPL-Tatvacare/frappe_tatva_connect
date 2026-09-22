// Shared Desk helpers for the account + settings forms (WhatsApp Account, CRM Telephony Account, CRM Push Settings), loaded on every Desk page via hooks.py `app_include_js` as a content-hashed bundle.
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
    primary_action: opts.action || undefined,
  });
}

// `tatva_pick_rows(opts)` — the one "here is what the provider reports, tick what to add" dialog; rows group under a heading, `group_pick` offers a tick that takes a whole group where that is valid, taken rows are shown but never offered, and a long list scrolls inside the dialog.
window.tatva_pick_rows = function tatva_pick_rows(opts) {
  const rows = opts.rows || [];
  const note = (o) =>
    (o.refused || []).length
      ? o.note + ' ' + __('Not answered for: {0}.', [(o.refused || []).map((r) => r.account + ' (' + r.reason + ')').join(', ')])
      : o.note;
  if (!rows.length) {
    frappe.msgprint({ title: opts.title, message: note(opts), indicator: 'orange' });
    return null;
  }

  const dialog = new frappe.ui.Dialog({
    title: opts.title,
    size: 'large',
    fields: [{ fieldname: 'rows', fieldtype: 'HTML' }],
    primary_action_label: opts.action_label,
    primary_action: () => {
      const picked = rows.filter((_r, i) => dialog.$wrapper.find('input[data-idx="' + i + '"]:checked').length);
      dialog.hide();
      if (picked.length) opts.on_pick(picked);
    },
  });

  const esc = (v) => frappe.utils.escape_html(String(v == null ? '' : v));
  const head = (opts.headers || []).map((h) => '<th class="text-muted" style="padding:4px 10px;text-align:left;font-weight:normal">' + esc(h) + '</th>').join('');
  const groups = [...new Set(rows.map((r) => r.group || ''))];
  const body = groups
    .map((group, g) => {
      const inside = rows.map((r, i) => [r, i]).filter(([r]) => (r.group || '') === group);
      const free = inside.filter(([r]) => !r.taken);
      const header = group
        ? '<tr style="background:var(--subtle-fg)"><td style="padding:6px 10px">' +
          (opts.group_pick && free.length ? '<input type="checkbox" data-group="' + g + '">' : '') +
          '</td><td colspan="' + (opts.headers || []).length + '" style="padding:6px 10px;font-weight:600">' + esc(group) + '</td></tr>'
        : '';
      const lines = inside
        .map(([r, i]) => {
          const tick = r.taken
            ? '<span class="text-muted">' + esc(r.taken_label) + '</span>'
            : '<input type="checkbox" data-idx="' + i + '" data-in-group="' + g + '">';
          return '<tr style="border-top:1px solid var(--border-color)' + (r.taken ? ';color:var(--text-muted)' : '') + '">' +
            '<td style="padding:4px 10px">' + tick + '</td>' + (r.cells || []).map((c) => '<td style="padding:4px 10px">' + esc(c) + '</td>').join('') + '</tr>';
        })
        .join('');
      return header + lines;
    })
    .join('');

  dialog.fields_dict.rows.$wrapper.html(
    '<div class="text-muted" style="margin-bottom:8px">' + esc(note(opts)) + '</div>' +
    '<div style="max-height:340px;overflow:auto">' +
    '<table style="width:100%;border-collapse:collapse"><thead><tr><th></th>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>'
  );
  dialog.$wrapper.on('change', 'input[data-group]', (e) => {
    const g = e.currentTarget.getAttribute('data-group');
    dialog.$wrapper.find('input[data-in-group="' + g + '"]').prop('checked', e.currentTarget.checked);
  });
  dialog.show();
  return dialog;
};

// One Validate Token button for every record that holds a Facebook credential. The caller supplies only
// what its token is FOR, because that is the sole difference between the three forms that offer it.
window.tatva_validate_token = function tatva_validate_token(frm, ok_title, fail_title) {
  frm.add_custom_button(__('Validate Token'), () => {
    frappe.call({
      method: 'tatva_connect.lead_sync.api.validate_token',
      args: { doctype: frm.doctype, name: frm.doc.name },
      freeze: true,
      freeze_message: __('Asking Facebook…'),
      callback: (r) => tatva_show_check_report(r.message || {}, {
        ok_title, fail_title, action: tatva_fb_refresh_action(frm),
      }),
    });
  });
};

// Whatever the verdict, the next step is the same: discovery is what stores the Page tokens a crawl runs on.
window.tatva_fb_refresh_action = function tatva_fb_refresh_action(frm) {
  const app = frm.doctype === 'CRM Facebook App' ? frm.doc.name : frm.doc.facebook_app;
  if (!app) return null;
  return {
    label: __('Refresh From Facebook'),
    action: () => frappe.confirm(tatva_fb_discovery_prompt(), () => tatva_fb_discover(app)),
  };
};

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

// One prompt and one call for `Refresh From Facebook`, shared by the app form and the Lead Forms list.
window.tatva_fb_discovery_prompt = function tatva_fb_discovery_prompt() {
  return __('Fetch this app\'s Pages and lead forms from Facebook? New forms are added, existing ones are left alone, and no leads are fetched.');
};

// `after` lets a list refresh itself once the answer is in.
window.tatva_fb_discover = function tatva_fb_discover(app, after) {
  frappe.call({
    method: 'tatva_connect.lead_sync.api.refresh_app',
    args: { app: app },
    freeze: true,
    freeze_message: __('Refreshing Pages and forms…'),
    callback: (r) => {
      const res = (r && r.message) || {};
      frappe.msgprint({
        title: __('Refreshed from Facebook'),
        indicator: 'green',
        message: __('{0} Page(s) and {1} form(s) are now current.', [res.pages || 0, res.forms || 0]),
      });
      if (typeof after === 'function') after(res);
    },
  });
};

// A source with no `last_synced_at` applies no created-after filter, so its first pass takes everything.
window.tatva_sync_now_prompt = function tatva_sync_now_prompt(doc) {
  // Enabled governs the SCHEDULED sync only, so a disabled source still syncs on demand and says so here.
  const disabled = doc && !doc.enabled ? __('This source is disabled. A manual sync still runs; only the scheduled sync is off.') + '<br><br>' : '';
  if (!doc || !doc.last_synced_at) {
    return disabled + __('This source has never run, so it will fetch every lead this form has ever collected. Assignment rules and notifications fire on each one.');
  }
  return disabled + __('Fetch leads created since {0}? Leads already here are skipped.', [frappe.datetime.str_to_user(doc.last_synced_at)]);
};

// Shared by the three account forms: replacing a token silently breaks a live webhook.
window.tatva_webhook_token_prompt = function tatva_webhook_token_prompt() {
  return __('Replace the webhook token? The provider keeps posting to the old URL until you re-register it.');
};
