// Shared Desk helpers for the account + settings forms (WhatsApp Account, CRM Telephony Account, CRM Push Settings), loaded on every Desk page via hooks.py `app_include_js` as a content-hashed bundle.
// `tatva_webhook_random_token()` — generate a URL-safe-ish secret for a fresh token.
// `tatva_webhook_actions(frm, token_field, banner)` — the one Webhook ▾ menu (Generate Token, Copy URL/URLs) + its banner, for every account form.
// `tatva_render_webhook_urls(frm, opts)` — fetch the REAL URL(s) server-side, paint the banner, and offer Copy only once they exist.
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

// `tatva_status_pill(word, colours)` — a provider's own word in frappe's list-view pill, or nothing when it said nothing.
window.tatva_status_pill = function tatva_status_pill(word, colours) {
  if (!word) return '';
  return `<span class="indicator-pill ${colours[word] || 'gray'}">${frappe.utils.escape_html(word)}</span>`;
};

// `tatva_wizard_shell(frm, opts)` — the ONE Data Import wizard shell every stepped Desk form builds on: frappe 16.52's markup, page classes, card geometry, step state and drawing; opts = { steps, label, fields, finished, on_step_click }.
window.tatva_wizard_shell = function tatva_wizard_shell(frm, opts) {
  const steps = opts.steps.map((label) => ({ label }));
  const state = { step: null, reached: 0, landing: 0, docname: null, last: steps.length - 1 };
  // A step past the furthest reached is locked; one before the landing step, or the last once the form says it finished, is done.
  const stepper = new frappe.ui.Stepper({
    steps, label: opts.label,
    is_locked: (i) => i > state.reached,
    is_completed: (i) => i < state.landing || (i === state.last && Boolean(opts.finished && opts.finished())),
    on_step_click: opts.on_step_click, on_locked_click: opts.on_step_click,
  });
  const compact = new frappe.ui.Stepper({ steps, label: opts.label, compact: true });
  frm.page.wrapper.addClass('data-import-custom-shell');
  frm.page.main.addClass('data-import-custom-page flex flex-col flex-1');
  frm.page.main.closest('.layout-main-section-wrapper').addClass('data-import-custom-wrapper');
  const $root = $(`
    <div class="data-import-custom-ui flex justify-center w-full">
      <div class="diw-shell flex flex-col w-full min-w-0 gap-5">
        <div class="diw-stepper-wrap shrink-0 w-full"></div>
        <div class="diw-card flex flex-col w-full min-w-0 overflow-hidden rounded-lg shadow-sm border bg-surface-base">
          <div class="diw-mobile-step-header shrink-0 pt-4 px-4"></div>
          <div class="flex flex-col flex-1 min-h-0 min-w-0 w-full"><div class="diw-panels flex flex-col flex-1 min-h-0 min-w-0 w-full"></div></div>
          <div class="diw-status-area shrink-0 px-5 pb-4"></div>
          <div class="diw-footer flex items-center justify-between shrink-0 w-full border-t mt-auto py-4 px-5">
            <div class="diw-footer-left"></div>
            <div class="diw-footer-right flex items-center gap-2"></div>
          </div>
        </div>
      </div>
    </div>`).prependTo(frm.page.main);
  $root.find('.diw-stepper-wrap').append(stepper.$el);
  $root.find('.diw-mobile-step-header').append(compact.$el);
  const $card = $root.find('.diw-card');
  const $panels = $root.find('.diw-panels');
  // frappe's DataImportWizard.update_card_height, its numbers as is: one card height from the viewport on every step.
  const fit = () => {
    const mobile = (window.innerWidth || 1024) <= 768;
    const height = Math.max(mobile ? 480 : 620, Math.min(820, (window.innerHeight || 768) - (mobile ? 145 : 205)));
    $card.css({ height: `${height}px`, 'min-height': `${height}px`, 'max-height': `${height}px` });
  };
  window.addEventListener('resize', fit, { passive: true });
  fit();
  return Object.assign(state, {
    stepper, compact,
    $left: $root.find('.diw-footer-left'),
    $right: $root.find('.diw-footer-right'),
    // New step labels when they depend on the record (a job picked on the form); both steppers redraw.
    set_steps(labels) {
      stepper.steps = compact.steps = labels.map((label) => ({ label }));
      this.last = labels.length - 1;
      stepper.refresh();
      compact.refresh();
    },
    // Land on the furthest step, follow the state forward, otherwise stay where the operator is; another record starts over.
    sync(reached, landing) {
      if (this.docname !== frm.doc.name) this.step = null;
      this.docname = frm.doc.name;
      this.step = this.step == null || landing > this.landing ? landing : Math.min(this.step, reached);
      Object.assign(this, { reached, landing });
    },
    // Move to step `i` and say so, or say what to do first when it is not reached yet.
    go(i, not_yet) {
      if (i > this.reached) {
        frappe.show_alert({ message: not_yet, indicator: 'orange' });
        return false;
      }
      this.step = Math.max(0, i);
      return true;
    },
    // Step `i`, as frappe's render_panel: the moved fields detached (not emptied, so they keep their handlers), a fresh panel drawn, dependencies re-read.
    render(i, draw) {
      stepper.set_current(i);
      compact.set_current(i);
      (opts.fields || []).forEach((f) => frm.fields_dict[f].$wrapper.detach());
      this.$progress = null;
      const $content = $('<div class="diw-step-content flex-1 min-h-0 min-w-0 w-full overflow-auto py-4 px-5">');
      $panels.empty().append($('<section class="diw-step-panel flex flex-col flex-1 min-h-0 min-w-0 w-full">').attr('data-step', i).append($content));
      draw($content); // a fresh panel each time: anything still loading for the last one lands off screen
      frm.layout.refresh_dependency();
    },
    // Next: save first when there is anything to save, then go as far as the saved state allows (frappe resolves a refused save too, leaving the form dirty).
    next(go) {
      const target = this.step + 1;
      if (!frm.is_new() && !frm.is_dirty()) return go(target);
      return frm.save().then(() => !frm.is_dirty() && go(Math.min(target, this.reached)));
    },
    // The footer, as frappe's render_footer: Back; on an action step, Save while edited, else the action; elsewhere Next. action = { label, run, icon, disabled, title } or null.
    footer(action, go) {
      let $right = null;
      if (action && frm.is_dirty()) $right = frappe.ui.button({ label: __('Save'), variant: 'solid', onclick: () => frm.save() });
      else if (action) $right = frappe.ui.button({ label: action.label, variant: 'solid', icon_left: action.icon || '', disabled: action.disabled, title: action.title || '', onclick: action.run });
      else if (this.step < this.last) $right = frappe.ui.button({ label: __('Next'), icon_right: 'arrow-right', onclick: () => this.next(go) });
      this.$left.empty().append(this.step > 0 ? frappe.ui.button({ label: __('Back'), icon_left: 'arrow-left', onclick: () => go(this.step - 1) }) : []);
      this.$right.empty().append($right || []);
    },
    // A run in flight: the progress slot its worker's events fill through show_progress.
    progress($c) {
      this.$progress = $('<div class="diw-import-progress-standard py-6">').appendTo($c);
    },
    // Draw `done` of `total` in the progress slot, when the current step has one.
    show_progress(label, done, total, unit) {
      if (!this.$progress) return;
      this.$progress.empty().append(frappe.ui.progress({ label, size: 'md', value: total ? (done * 100) / total : 0, hint: total ? () => __('{0} of {1} {2}', [done, total, unit]) : false }));
    },
    // A Data Import section heading, with an optional action on its right.
    head(title, $action) {
      return $('<div class="flex items-center justify-between gap-4 mb-4">')
        .append($('<span class="diw-section-head-title text-base-semibold">').text(title), $action || []);
    },
    // A section as every step reads: heading (with an optional action), one hint line, then its content.
    section(title, hint, content, $action) {
      return $('<div class="diw-config-section">').append(this.head(title, $action), $('<p class="text-sm text-ink-gray-6 mb-3">').text(hint), content);
    },
    // Frappe's Fix issues summary strip; each cell is [value, label, extra class].
    stats(cells) {
      const cell = ([value, label, css = ''], i) => `
        <div class="flex flex-col items-center justify-center flex-1 gap-1 p-4${i ? ' diw-fix-empty-stat--divided border-s' : ''}" role="listitem">
          <div class="text-2xl-semibold text-ink-gray-8 ${css}">${frappe.utils.escape_html(String(value ?? 0))}</div>
          <div class="text-sm text-ink-gray-5 text-center">${frappe.utils.escape_html(label)}</div>
        </div>`;
      return `<div class="diw-fix-empty-stats flex w-full max-w-4xl border rounded-lg overflow-hidden bg-surface-base mt-2" role="list">${cells.map(cell).join('')}</div>`;
    },
    // Data Import's log table: headers are [label, width], each row its cells' html; its empty state when there are none.
    table(headers, rows, empty) {
      if (!rows.length) return `<div class="diw-import-log-empty-state">${frappe.ui.empty_state.html({ icon: 'inbox', title: empty, css_class: 'min-h-32' })}</div>`;
      const th = headers.map(([label, width]) => `<th class="text-sm-semibold border-b py-2 px-4" width="${width}">${frappe.utils.escape_html(label)}</th>`).join('');
      const tr = rows.map((cells) => `<tr>${cells.map((html, i) => `<td class="text-sm align-top border-b py-2 px-4${i ? ' break-words' : ' whitespace-nowrap'}">${html}</td>`).join('')}</tr>`).join('');
      return `<table class="diw-import-log-table w-full"><thead><tr class="text-ink-gray-6">${th}</tr></thead><tbody>${tr}</tbody></table>`;
    },
    // Moves the form's own fields into a slot of the current panel, as frappe's reparent_field.
    place($into, fieldnames) {
      fieldnames.forEach((f) => $into.append(frm.fields_dict[f].$wrapper));
      return $into;
    },
    // Frappe's Config grid: these fields on the left, those on the right.
    columns(left, right) {
      return $('<div class="diw-config-grid grid items-start w-full gap-5">').append(
        this.place($('<div class="diw-config-column diw-config-main w-full min-w-0">'), left),
        this.place($('<div class="diw-config-column diw-config-options w-full min-w-0">'), right));
    },
  });
};

// `tatva_history_button(frm)` — a wizard form hides its timeline (hide_toolbar), so its Version list stays one click away for whoever may read it.
window.tatva_history_button = function tatva_history_button(frm) {
  if (frm.is_new() || !frappe.model.can_read('Version')) return;
  frm.add_custom_button(__('History'), () => frappe.set_route('List', 'Version', { ref_doctype: frm.doctype, docname: frm.doc.name }));
};

// `tatva_pick_rows(opts)` —the one "here is what the provider reports, tick what to add" dialog, drawn by frappe's own in-dialog grid; a row someone already holds carries its Status and is never added, and the grid pages itself.
window.tatva_pick_rows = function tatva_pick_rows(opts) {
  const rows = (opts.rows || []).map((r, i) => ({ ...r, idx: i + 1 }));
  const refused = opts.refused || [];
  const note = refused.length
    ? opts.note + ' ' + __('Could not read {0}: the provider refused.', [refused.map((r) => r.account).join(', ')])
    : opts.note;
  if (!rows.length) {
    frappe.msgprint({ title: opts.title, message: note, indicator: 'orange' });
    return null;
  }

  const dialog = new frappe.ui.Dialog({
    title: opts.title,
    size: 'large',
    fields: [
      { fieldtype: 'HTML', fieldname: 'note', options: `<p class="text-muted">${frappe.utils.escape_html(note)}</p>` },
      {
        fieldtype: 'Table',
        fieldname: 'rows',
        read_only: 1,
        cannot_add_rows: 1,
        cannot_delete_rows: 1,
        in_place_edit: 0,
        fields: opts.columns,
        data: rows,
        get_data: () => rows,
      },
    ],
    primary_action_label: opts.action_label,
    primary_action: () => {
      const picked = rows.filter((r) => r.__checked && !r.taken); // the grid's own tick, read off every row: get_selected_children sees only the rows a filter leaves
      dialog.hide();
      if (picked.length) opts.on_pick(picked);
    },
  });
  dialog.onhide = () => dialog.$wrapper.remove(); // a dialog is built per ask; frappe leaves the closed one in the DOM
  dialog.show();
  // an in-dialog grid has no doctype meta: its pagination defaults to 50 rows, and a dialog grid reads as editable whatever the docfield says
  const grid = dialog.fields_dict.rows.grid;
  grid.grid_pagination.page_length = 8;
  grid.only_sortable(); // a picker offers no row editing — frappe hides add, upload and delete once the rows are static
  grid.refresh();
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

// A saved token reads back as `***`, so "is one set" is truthiness; replacing a live one asks first, a first one does not.
window.tatva_webhook_actions = function tatva_webhook_actions(frm, token_field, banner) {
  const generate = () => {
    frm.set_value(token_field, tatva_webhook_random_token());
    frappe.show_alert({ message: __('Webhook token generated — Save to apply.'), indicator: 'green' });
  };
  frm.add_custom_button(__('Generate Token'), () => (frm.doc[token_field] ? frappe.confirm(tatva_webhook_token_prompt(), generate) : generate()), __('Webhook'));
  tatva_render_webhook_urls(frm, banner);
};

// opts: { single_label (one URL), multi_label (several URLs), register_hint (the trailing dashboard instruction) }.
window.tatva_render_webhook_urls = function tatva_render_webhook_urls(frm, opts) {
  frm.dashboard.clear_headline();
  frm.remove_custom_button(__('Copy URL'), __('Webhook'));
  frm.remove_custom_button(__('Copy URLs'), __('Webhook'));
  if (frm.is_new()) return;

  frappe.call({
    method: 'tatva_connect.webhooks.urls.get_account_webhook_urls',
    args: { account_doctype: frm.doctype, name: frm.doc.name },
    callback: (r) => {
      const res = r.message || {};
      const urls = res.urls || [];
      const targets = res.targets || [];
      if (!res.token_set || !targets.length) return;

      // Copy appears only once the server has returned URLs, so it never copies nothing, and its label states how many.
      frm.add_custom_button(urls.length > 1 ? __('Copy URLs') : __('Copy URL'), () => frappe.utils.copy_to_clipboard(urls.join('\n')), __('Webhook'));

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

// `Refresh From WhatsApp`, shared by the WhatsApp Templates list and the WhatsApp Account form: one account per refresh.
window.tatva_wa_refresh_templates = function tatva_wa_refresh_templates(account, after) {
  frappe.call({
    method: 'tatva_connect.whatsapp.templates_sync.sync_templates',
    args: { account_name: account },
    freeze: true,
    freeze_message: __('Refreshing templates for {0}…', [account]),
    callback: (r) => {
      frappe.msgprint({ title: __('Templates refreshed'), indicator: 'green', message: r.message || '' });
      if (typeof after === 'function') after(r.message);
    },
  });
};

// `tatva_merge_button(frm)` — fold this record into another through frappe's own rename-merge; the person ticks which of its values and rows come across.
window.tatva_merge_button = function tatva_merge_button(frm) {
  if (frm.is_new() || !frappe.model.can_delete(frm.doctype)) return;
  const api = 'tatva_connect.merge.api.';
  const esc = (v) => frappe.utils.escape_html(v === null || v === undefined || v === '' ? __('empty') : String(v));
  const review = (keep, plan) => {
    const moves = Object.entries(plan.moves).map(([dt, n]) => `${esc(dt)}: ${n}`).join(', ') || __('nothing');
    const fields = plan.fields.map((f) => ({
      fieldtype: 'Check', fieldname: `f__${f.fieldname}`, default: f.keep ? 0 : 1,
      label: `${esc(f.label)}: ${esc(f.drop)}`, description: __('Now {0}', [esc(f.keep)]),
    }));
    const tables = plan.tables.map((t) => ({
      fieldtype: 'Check', fieldname: `t__${t.fieldname}`, default: 1, label: __('{0}: {1} rows', [esc(t.label), t.rows]),
    }));
    const empty = plan.fields.filter((f) => !f.keep).map((f) => `f__${f.fieldname}`).concat(tables.map((t) => t.fieldname));
    const dialog = new frappe.ui.Dialog({
      title: __('Merge {0} into {1}', [esc(frm.doc.name), esc(keep)]),
      size: 'large',
      fields: [
        { fieldtype: 'HTML', fieldname: 'note', options: `<p>${__('{0} will be deleted and cannot be restored. Everything linked to it moves to {1}: {2}.', [esc(frm.doc.name), esc(keep), moves])}</p>` },
        { fieldtype: 'Check', fieldname: 'everything', default: 1, label: __('Bring everything across'), description: __('Fills every empty field and adds every row. A field both records hold keeps its value unless ticked below.'),
          onchange: () => empty.forEach((name) => dialog.set_value(name, dialog.get_value('everything'))) },
        ...(fields.length ? [{ fieldtype: 'Section Break', label: __('Values') }, ...fields] : []),
        ...(tables.length ? [{ fieldtype: 'Section Break', label: __('Rows') }, ...tables] : []),
      ],
      primary_action_label: __('Merge'),
      primary_action: (values) => {
        const picked = (prefix) => Object.keys(values).filter((k) => k.startsWith(prefix) && values[k]).map((k) => k.slice(prefix.length));
        frappe.confirm(__('Delete {0} and merge it into {1}? This cannot be undone.', [esc(frm.doc.name), esc(keep)]), () =>
          frappe.call({ method: api + 'merge', args: { doctype: frm.doctype, keep, drop: frm.doc.name, take: picked('f__'), carry: picked('t__') }, freeze: true })
            .then(() => { dialog.hide(); frappe.set_route('Form', frm.doctype, keep); }));
      },
    });
    dialog.onhide = () => dialog.$wrapper.remove(); // a dialog is built per ask; frappe leaves the closed one in the DOM
    dialog.show();
  };
  frm.add_custom_button(__('Merge'), () => {
    const pick = new frappe.ui.Dialog({
      title: __('Merge {0} into', [esc(frm.doc.name)]),
      fields: [{ fieldtype: 'Link', fieldname: 'keep', options: frm.doctype, reqd: 1, label: __('Record to keep'),
        get_query: () => ({ filters: { name: ['!=', frm.doc.name] } }) }],
      primary_action_label: __('Review'),
      primary_action: ({ keep }) => frappe.call({ method: api + 'preview', args: { doctype: frm.doctype, keep, drop: frm.doc.name } })
        .then(({ message }) => { pick.hide(); review(keep, message); }),
    });
    pick.onhide = () => pick.$wrapper.remove();
    pick.show();
  }, __('Actions'));
};
