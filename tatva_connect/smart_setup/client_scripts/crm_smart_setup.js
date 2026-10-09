// Desk Client Script — CRM Smart Setup, on the CRM Lead Import pattern: one action per stage, realtime progress, one reload.

const TATVA_SS_API = 'tatva_connect.smart_setup.api.';

// What each result reads as in the Records Checked grid.
const TATVA_SS_INDICATOR = { created: 'green', updated: 'blue', unchanged: 'gray', kept: 'orange', removed: 'purple', refused: 'red' };

// What each part's status reads as in a Check.
const TATVA_SS_STATUS = { Done: 'green', 'Switched Off': 'orange', Missing: 'red', 'Not Set Up': 'gray' };

frappe.ui.form.on('CRM Smart Setup', {
  setup(frm) {
    frappe.realtime.on('smart_setup_progress', (data) => tatva_ss_progress(frm, data));
    frappe.realtime.on('smart_setup_refresh', ({ setup }) => {
      if (setup === frm.doc.name) frm.reload_doc();
    });
    frm.set_indicator_formatter('record', (row) => TATVA_SS_INDICATOR[row.action]);
    frappe.model.with_doctype('CRM Smart Setup Part', () => {
      frappe.meta.get_docfield('CRM Smart Setup Part', 'status').formatter = (value) =>
        value ? `<span class="indicator-pill ${TATVA_SS_STATUS[value] || 'gray'}">${frappe.utils.escape_html(__(value))}</span>` : '';
    });
    $(frm.wrapper).on('dirty', () => frm.enable_save());
  },

  onload(frm) {
    frappe.call(TATVA_SS_API + 'recipe_options').then((r) => {
      frm.tatva_ss_recipes = r.message || [];
      frm.set_df_property('recipe', 'options', ['', ...frm.tatva_ss_recipes.map((x) => x.recipe)].join('\n'));
      frm.refresh();
    });
  },

  refresh(frm) {
    tatva_step_intro(frm, tatva_ss_next_step(frm));
    tatva_ss_action(frm);
    if (frm.doc.direction === 'Copy') tatva_ss_copy_counts(frm);
    if (frm.doc.direction === 'Check') tatva_ss_check_counts(frm);
    if (frm.doc.direction === 'Copy') tatva_ss_by_hand(frm);
  },

  recipe(frm) {
    frm.clear_table('roots'); // records of one recipe are not records of another
    frm.refresh_field('roots');
  },
});

frappe.ui.form.on('CRM Smart Setup Root', {
  roots_add(frm, cdt, cdn) {
    frappe.model.set_value(cdt, cdn, 'root_doctype', tatva_ss_recipe(frm).root);
  },
});

// The one line that says where the operator is and what to do next.
function tatva_ss_next_step(frm) {
  const d = frm.doc;
  const said = d.__onload || {};
  if (frm.is_new()) return __('Choose Check to see what a grain still needs, Copy to put one grain\'s setup onto another, or Export and Import to move a setup between sites. Then save.');
  if (d.direction === 'Check') return tatva_ss_check_step(d);
  if (said.cut_off) return __('The last run stopped before it finished. Start it again.');
  if (d.direction === 'Export') {
    if (d.status === 'Draft' && !(d.roots || []).length) return __('Add the records to export, then save.');
    return {
      Draft: __('Build the bundle. Each record comes with everything it depends on.'),
      Building: __('Building the bundle.'),
      Built: __('Built: {0} in the bundle. Download it, then import it on the other site.', [d.record_count]),
      'Build Failed': __('The build stopped. The reason is under Error.'),
    }[d.status];
  }
  if (d.direction === 'Copy' && !d.restores) return tatva_ss_copy_step(d, said.labels || {});
  if (!d.bundle_file) return __('Attach the bundle file an export produced, then save.');
  if (d.restores && d.status === 'Checked') {
    return __('Ready to restore: {0} to put back, {1} to remove, {2} unchanged. Restore writes them all, or none.', [tatva_ss_put_back(d), d.removed_count, d.unchanged_count]);
  }
  if (d.restores && d.status === 'Applied') {
    return __('Restored: {0} put back, {1} removed. The {2} import applied on {3} is undone.', [tatva_ss_put_back(d), d.removed_count, frappe.utils.escape_html(d.recipe), frappe.datetime.str_to_user(d.exported_at)]);
  }
  return {
    Draft: __('Check the bundle. Every record is saved as it would be and then rolled back, so nothing changes yet.'),
    Checking: __('Checking: {0} in the bundle. Nothing is written.', [d.record_count]),
    Checked: __('Ready: {0} to create, {1} to update, {2} unchanged, {3} kept as this site has them. Apply writes them all, or none.', [d.created_count, d.updated_count, d.unchanged_count, d.kept_count]),
    'Check Failed': d.error ? __('The check stopped. The reason is under Error.')
      : __('Refused: {0}. Each refused record says why under Records Checked. Fix them, then check again.', [d.refused_count]),
    Applying: __('Applying.'),
    Applied: (said.labels || {}).recipe === (said.labels || {}).workflows
      ? __('Applied: {0} created, {1} updated. Workflows arrive as Drafts: publish them on this site.', [d.created_count, d.updated_count])
      : __('Applied: {0} created, {1} updated. The setup is live on this site.', [d.created_count, d.updated_count]),
    'Apply Failed': __('Nothing was applied. The reason is under Records Checked or Error. Fix it, then check again.'),
  }[d.status];
}

// The Copy flow's one line: what to do, what the run will do, and what is left once it has.
function tatva_ss_copy_step(d, labels) {
  if (!d.source_grain || !d.target_grain) return __('Pick the grain to copy from and the grain to copy to, then save.');
  const waiting = tatva_ss_waiting(d, labels.ready);
  const source = frappe.utils.escape_html(d.source_grain);
  const target = frappe.utils.escape_html(d.target_grain);
  return {
    Draft: __('The counts above are what a copy of {0} carries onto {1}. Press Preview to see every record; nothing is written yet.', [source, target]),
    Checking: __('Previewing the copy. Nothing is written.'),
    Checked: __('Ready: {0} to create on {1}, {2} already there and kept. Each row shows its name before and after, and how it lands. Press Copy to write them all, or none.', [d.created_count, target, d.kept_count + d.unchanged_count]),
    'Check Failed': d.error ? __('The preview stopped. The reason is under Error.')
      : __('Blocked: {0}. Each blocked row names what this site lacks. Create it, then press Preview again.', [d.refused_count]),
    Applying: __('Copying.'),
    Applied: __('Copied: {0} records onto {1}.', [d.created_count, target]) + ' ' + ({
      0: '',
      1: __('One lands switched off or as a draft, shown under Lands As. Turn it on once it is signed off.'),
    }[waiting] ?? __('{0} land switched off or as drafts, shown under Lands As. Turn each on once it is signed off.', [waiting]))
      + ' ' + __('Then do the steps under Still to Do by Hand.'),
    'Apply Failed': __('Nothing was copied. The reason is under Records Checked or Error. Fix it, then press Preview again.'),
  }[d.status];
}

// The Check flow's one line: where the grain stands, and what to do about it.
function tatva_ss_check_step(d) {
  if (!d.check_grain) return __('Pick the grain to check, then save.');
  const n = (status) => (d.parts || []).filter((row) => row.status === status).length;
  return __('Checked {0} at {1}: {2} done, {3} switched off, {4} missing. Each row under Parts says what to do. Press Recheck after a change.',
    [frappe.utils.escape_html(d.check_grain), frappe.datetime.str_to_user(d.checked_at), n('Done'), n('Switched Off'), n('Missing')]);
}

// Counts by status above a Check's parts.
function tatva_ss_check_counts(frm) {
  const by = {};
  (frm.doc.parts || []).forEach((row) => { by[row.status] = (by[row.status] || 0) + 1; });
  Object.entries(by).forEach(([status, n]) => frm.dashboard.add_indicator(`${__(status)}: ${n}`, TATVA_SS_STATUS[status]));
}

// The parts a copy never carries, from the server's one list, each linked to its list and its handbook page.
function tatva_ss_by_hand(frm) {
  const rows = (frm.doc.__onload || {}).by_hand || [];
  const esc = frappe.utils.escape_html;
  frm.get_field('by_hand').$wrapper.html(`<p class="text-muted">${esc(__('A copy carries the grain\'s structure. These parts hold people, credentials and outside accounts, so they stay with you.'))}</p><ul>${
    rows.map((r) => `<li><a href="${esc(r.route)}">${esc(r.label)}</a> · <a href="/crm-configuration/runbooks/new-grain/${esc(r.step)}" target="_blank">${esc(__('How to set it up'))}</a></li>`).join('')}</ul>`);
}

// How many copied records land switched off, as a draft or disabled, so nothing acts before sign-off.
function tatva_ss_waiting(d, ready) {
  return (d.items || []).filter((row) => row.action === 'created' && row.arrives_as && row.arrives_as !== ready).length;
}

// Counts by type: before a preview, what the copy would carry; after one, what each row became.
function tatva_ss_copy_counts(frm) {
  const d = frm.doc;
  if (frm.is_new() || !d.source_grain || !d.target_grain) return;
  if (!(d.items || []).length) {
    const draw = (frm.tatva_ss_draw = (frm.tatva_ss_draw || 0) + 1); // a save refreshes twice; only the latest call draws
    frappe.call({ method: TATVA_SS_API + 'copy_scope', args: { setup: d.name } }).then(({ message }) => {
      if (draw !== frm.tatva_ss_draw) return;
      Object.entries(message || {}).forEach(([doctype, n]) => frm.dashboard.add_indicator(`${__(doctype)}: ${n}`, 'blue'));
    });
    return;
  }
  const by = {};
  d.items.forEach((row) => {
    const key = row.action === 'refused' ? 'refused' : row.action === 'created' ? 'created' : 'kept';
    const part = row.part || row.ref_doctype;
    by[part] = by[part] || { created: 0, kept: 0, refused: 0 };
    by[part][key] += 1;
  });
  Object.entries(by).forEach(([doctype, n]) => {
    const parts = [n.created && __('{0} new', [n.created]), n.kept && __('{0} kept', [n.kept]), n.refused && __('{0} blocked', [n.refused])];
    frm.dashboard.add_indicator(`${__(doctype)}: ${parts.filter(Boolean).join(', ')}`, n.refused ? 'red' : n.created ? 'green' : 'gray');
  });
}

// What a Copy confirm lists: the records it will write, by type.
function tatva_ss_copy_summary(d) {
  const by = {};
  (d.items || []).filter((row) => row.action === 'created').forEach((row) => { const part = row.part || row.ref_doctype; by[part] = (by[part] || 0) + 1; });
  return Object.entries(by).map(([part, n]) => `${frappe.utils.escape_html(part)}: ${n}`).join('<br>');
}

// One action per stage in place of Save, as the server names it; an edit brings Save back until it is saved.
function tatva_ss_action(frm) {
  if (frm.is_new() || frm.is_dirty()) return;
  frm.disable_save(true);
  const d = frm.doc;
  if (d.direction === 'Check') {
    frm.page.set_primary_action(__('Recheck'), () => { frm.dirty(); frm.save(); }); // the save is what reads the grain again
    return;
  }
  const said = d.__onload || {};
  const stage = said.next_stage;
  if (stage === 'apply' && d.direction === 'Copy' && !d.restores) {
    frm.page.set_primary_action(__('Copy'), () => frappe.confirm(
      __('Copy these onto {0}? Nothing on {1} changes.', [frappe.utils.escape_html(d.target_grain), frappe.utils.escape_html(d.source_grain)]) + '<br><br>' + tatva_ss_copy_summary(d),
      () => tatva_ss_run(frm, 'apply')));
  } else if (stage === 'apply') {
    frm.page.set_primary_action(d.restores ? __('Restore') : __('Apply'), () => frappe.confirm(d.restores
      ? __('To put back: {0}. To remove: {1}. Restore?', [tatva_ss_put_back(d), d.removed_count])
      : __('To create: {0}. To update: {1}. Nothing is deleted. Apply?', [d.created_count, d.updated_count]),
      () => tatva_ss_run(frm, 'apply')));
  } else if (stage) {
    frm.page.set_primary_action({ build: __('Build'), check: __('Check'), preview: __('Preview') }[stage], () => tatva_ss_run(frm, stage));
  } else if (d.status === 'Built') {
    // The SAVE flavour of the same link: served plain, a browser renders the bundle instead of saving it.
    frm.page.set_primary_action(__('Download'), () => window.open(tatva_ss_download_url(d.bundle_file)));
  }
  if (d.direction === 'Copy' && d.status === 'Applied') {
    frm.add_custom_button(__('Check the Grain'), () => frappe.new_doc('CRM Smart Setup', { direction: 'Check', check_grain: d.target_grain }));
  }
  if (said.can_restore) {
    frm.add_custom_button(__('Restore Prior Version'), () => frappe.confirm(
      __('Put this site back as it was before this apply? A Check runs first and nothing is written until you press Restore.'),
      () => frappe.call({ method: TATVA_SS_API + 'restore', args: { setup: d.name }, freeze: true })
        .then(({ message }) => frappe.set_route('Form', 'CRM Smart Setup', message))));
  }
  if (d.direction === 'Export' && d.status === 'Draft' && tatva_ss_recipe(frm).by_grain) {
    frm.add_custom_button(__('Add All for a Product Line'), () => tatva_ss_add_all(frm));
  }
}

// What a restore writes back: each record an apply updated returns to its prior version, each it removed returns.
function tatva_ss_put_back(d) {
  return d.updated_count + d.created_count;
}

function tatva_ss_recipe(frm) {
  return (frm.tatva_ss_recipes || []).find((r) => r.recipe === frm.doc.recipe) || {};
}

function tatva_ss_run(frm, stage) {
  frappe.call({ method: TATVA_SS_API + 'start', args: { setup: frm.doc.name, stage }, freeze: true })
    .then(() => frm.reload_doc());
}

// Every record of the recipe on one product line, group and program, added to the grid once.
function tatva_ss_add_all(frm) {
  const dialog = new frappe.ui.Dialog({
    title: __('Add all for a product line'),
    fields: [
      { fieldname: 'vertical', fieldtype: 'Link', options: 'CRM Vertical', label: __('Product Line'), reqd: 1 },
      { fieldname: 'group', fieldtype: 'Link', options: 'CRM Group', label: __('Group') },
      { fieldname: 'program', fieldtype: 'Link', options: 'CRM Program', label: __('Program') },
    ],
    primary_action_label: __('Add'),
    primary_action(values) {
      frappe.call(TATVA_SS_API + 'roots_for_grain', { recipe: frm.doc.recipe, ...values }).then((r) => {
        const have = new Set((frm.doc.roots || []).map((row) => row.record));
        const added = (r.message || []).filter((name) => !have.has(name));
        added.forEach((name) => frm.add_child('roots', { root_doctype: tatva_ss_recipe(frm).root, record: name }));
        frm.refresh_field('roots');
        dialog.hide();
        frappe.show_alert({ message: __('Added: {0}.', [added.length]), indicator: added.length ? 'green' : 'orange' });
      });
    },
  });
  dialog.show();
}

// The worker announces each record; draw it only while this setup is checking or applying.
function tatva_ss_progress(frm, { setup, done, total }) {
  if (setup !== frm.doc.name || !['Checking', 'Applying'].includes(frm.doc.status)) return;
  const copy = frm.doc.direction === 'Copy';
  const title = frm.doc.status === 'Applying' ? (copy ? __('Copying') : __('Applying')) : (copy ? __('Previewing') : __('Checking'));
  frm.dashboard.show_progress(title, (done * 100) / total, __('{0} of {1} records', [done, total]));
}

// The one place the Download control asks for the attachment flavour; `?` or `&` depending on what the stored url already carries.
function tatva_ss_download_url(url) {
  return `${url}${url.includes('?') ? '&' : '?'}download=1`;
}
