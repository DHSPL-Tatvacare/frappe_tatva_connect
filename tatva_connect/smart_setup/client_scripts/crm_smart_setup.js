// Desk Client Script — CRM Smart Setup, on the shared Data Import wizard shell: each job (Copy, Import, Export, Check) its own steps.

const TATVA_SS_API = 'tatva_connect.smart_setup.api.';

// What each result reads as in the Records Checked grid.
const TATVA_SS_INDICATOR = { created: 'green', updated: 'blue', unchanged: 'gray', kept: 'orange', removed: 'purple', refused: 'red' };

// What each part's status reads as in a Check.
const TATVA_SS_STATUS = { Done: 'green', 'Switched Off': 'orange', Missing: 'red', 'Not Set Up': 'gray' };

const TATVA_SS_INPUTS = ['recipe', 'source_grain', 'target_grain', 'bundle_file', 'roots'];
const TATVA_SS_FIELDS = ['direction', 'recipe', 'source_grain', 'target_grain', 'check_grain', 'bundle_file', 'record_count', 'source_site', 'exported_at', 'restores', 'restored_by', 'roots', 'parts', 'by_hand', 'items'];

frappe.ui.form.on('CRM Smart Setup', {
  setup(frm) {
    frappe.realtime.on('smart_setup_progress', (data) => tatva_ss_progress(frm, data));
    frappe.realtime.on('smart_setup_refresh', ({ setup }) => {
      if (setup === frm.doc.name) frm.reload_doc();
    });
    frm.set_indicator_formatter('record', (row) => TATVA_SS_INDICATOR[row.action]);
    frappe.model.with_doctype('CRM Smart Setup Part', () => {
      frappe.meta.get_docfield('CRM Smart Setup Part', 'status').formatter = (value) => tatva_status_pill(value, TATVA_SS_STATUS);
    });
    $(frm.wrapper).on('dirty', () => frm._ss_wizard && tatva_ss_footer(frm));
  },

  onload(frm) {
    frappe.call(TATVA_SS_API + 'recipe_options').then((r) => {
      frm.tatva_ss_recipes = r.message || [];
      frm.set_df_property('recipe', 'options', ['', ...frm.tatva_ss_recipes.map((x) => x.recipe)].join('\n'));
      frm.refresh();
    });
  },

  refresh(frm) {
    tatva_ss_lock(frm);
    tatva_ss_wizard(frm);
    tatva_history_button(frm);
  },

  direction(frm) {
    tatva_ss_wizard(frm); // the job decides the steps
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

// Inputs the server refuses to change while a run reads them and once applied (validate's _hold_while_running, _hold_once_applied).
function tatva_ss_lock(frm) {
  const d = frm.doc;
  frm.toggle_enable(TATVA_SS_INPUTS, !(d.status === 'Applied' || (d.__onload || {}).running)); // running: the server's api.is_running
  if (d.restores) frm.toggle_enable('bundle_file', false); // a restore runs its own restore point (server: _hold_restore_file)
}

// What to do first, said when the operator reaches for a step the setup has not reached.
function tatva_ss_not_yet(frm) {
  const d = frm.doc;
  if (frm.is_new()) return __('Pick what to do and fill in what it needs, then press Next.');
  if (d.direction === 'Export') return __('Add the records, then Build. The bundle opens once the build runs.');
  if (!frm._ss_wizard.reached) return __('Attach the bundle file an export produced.'); // a saved Copy always has both grains
  return d.direction === 'Copy' ? __('Copy from Fix issues; this step opens once the copy runs.') : __('Apply from Fix issues; this step opens once it runs.');
}

// The one action the reached step offers: the server's next stage, a Recheck, or the built bundle's Download.
function tatva_ss_action(frm) {
  const d = frm.doc;
  const stage = (d.__onload || {}).next_stage;
  if (d.direction === 'Check') return { label: __('Recheck'), run: () => { frm.dirty(); frm.save(); } }; // the save is what reads the grain again
  if (stage) {
    const retry = d.status.endsWith('Failed');
    const label = retry ? __('Retry') : { build: __('Build'), preview: __('Preview'), check: __('Check'),
      apply: d.direction === 'Copy' ? __('Copy') : d.restores ? __('Restore') : __('Apply') }[stage];
    return { label, icon: retry ? 'refresh-cw' : '', run: () => (stage === 'apply' ? tatva_ss_confirm_apply(frm) : tatva_ss_run(frm, stage)) };
  }
  // The SAVE flavour of the same link: served plain, a browser renders the bundle instead of saving it.
  if (d.status === 'Built') return { label: __('Download'), icon: 'download', run: () => window.open(tatva_ss_download_url(d.bundle_file)) };
  return null;
}

// The write, confirmed with what it writes.
function tatva_ss_confirm_apply(frm) {
  const d = frm.doc;
  const esc = frappe.utils.escape_html;
  const ask = d.direction === 'Copy'
    ? __('Copy these onto {0}? Nothing on {1} changes.', [esc(d.target_grain), esc(d.source_grain)]) + '<br><br>' + tatva_ss_copy_summary(d)
    : d.restores ? __('To put back: {0}. To remove: {1}. Restore?', [tatva_ss_put_back(d), d.removed_count])
      : d.removed_count ? __('To create: {0}. To update: {1}. To remove: {2}. Apply?', [d.created_count, d.updated_count, d.removed_count])
        : __('To create: {0}. To update: {1}. Nothing is deleted. Apply?', [d.created_count, d.updated_count]);
  frappe.confirm(ask, () => tatva_ss_run(frm, 'apply'));
}

// What a Copy confirm lists: the records it will write, by type.
function tatva_ss_copy_summary(d) {
  const by = {};
  (d.items || []).filter((row) => row.action === 'created').forEach((row) => { const part = row.part || row.ref_doctype; by[part] = (by[part] || 0) + 1; });
  return Object.entries(by).map(([part, n]) => `${frappe.utils.escape_html(part)}: ${n}`).join('<br>');
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

// What a copy would carry, by part; asked once per saved version, as the preview and the fix-issues strip both read it.
function tatva_ss_scope(frm) {
  const key = `${frm.doc.name}|${frm.doc.modified}`;
  if (!frm._ss_scope || frm._ss_scope.key !== key) {
    frm._ss_scope = { key, call: frappe.call({ method: TATVA_SS_API + 'copy_scope', args: { setup: frm.doc.name } }).then((r) => r.message || {}) };
  }
  return frm._ss_scope.call;
}

// Each job's steps and the drawing of each; a new setup before its job is picked has Setup only.
const TATVA_SS_FLOW = {
  Copy: { steps: [__('Setup'), __('Preview'), __('Fix issues'), __('Copy')], draw: [tatva_ss_setup, tatva_ss_copy_preview, tatva_ss_fix_issues, tatva_ss_outcome] },
  Import: { steps: [__('Setup'), __('Preview'), __('Fix issues'), __('Import')], draw: [tatva_ss_setup, tatva_ss_import_preview, tatva_ss_fix_issues, tatva_ss_outcome] },
  Export: { steps: [__('Setup'), __('Records'), __('Bundle')], draw: [tatva_ss_setup, tatva_ss_records, tatva_ss_bundle] },
  Check: { steps: [__('Setup'), __('Checklist')], draw: [tatva_ss_setup, tatva_ss_checklist] },
  '': { steps: [__('Setup')], draw: [tatva_ss_setup] },
};

function tatva_ss_flow(frm) {
  return TATVA_SS_FLOW[frm.doc.direction || ''];
}

// The furthest step this setup's state has reached.
function tatva_ss_reached(frm) {
  const d = frm.doc;
  if (frm.is_new()) return 0;
  if (d.direction === 'Check') return 1;
  if (d.direction === 'Export') return d.status === 'Draft' ? 1 : 2;
  if (!(d.direction === 'Copy' ? d.source_grain && d.target_grain : d.bundle_file)) return 0;
  return (d.__onload || {}).write_started ? 3 : 2;
}

// The step the form opens on: a Copy or Import not yet checked on Preview, otherwise the furthest reached.
function tatva_ss_landing(frm) {
  const reached = tatva_ss_reached(frm);
  return reached === 2 && frm.doc.status === 'Draft' ? 1 : reached;
}

// The last step is done once its job finished: applied, built, or checked.
function tatva_ss_finished(frm) {
  const d = frm.doc;
  return d.direction === 'Check' ? Boolean(d.checked_at) : d.status === (d.direction === 'Export' ? 'Built' : 'Applied');
}

// Built once per form; the steps follow the job, and the shell lands, follows and stays by this setup's reached and landing steps.
function tatva_ss_wizard(frm) {
  const w = frm._ss_wizard || (frm._ss_wizard = tatva_wizard_shell(frm, {
    steps: tatva_ss_flow(frm).steps,
    label: __('Smart Setup steps'),
    fields: TATVA_SS_FIELDS,
    finished: () => tatva_ss_finished(frm),
    on_step_click: (i) => tatva_ss_go(frm, i),
  }));
  if (!w) return;
  w.set_steps(tatva_ss_flow(frm).steps);
  w.sync(tatva_ss_reached(frm), tatva_ss_landing(frm));
  tatva_ss_render(frm);
}

// Redraw the current step through the shell, then the footer.
function tatva_ss_render(frm) {
  const w = frm._ss_wizard;
  w.render(w.step, ($c) => tatva_ss_flow(frm).draw[w.step](frm, $c));
  tatva_ss_footer(frm);
}

// Setup: the job, picked once on a new setup, then what that job needs.
function tatva_ss_setup(frm, $c) {
  const w = frm._ss_wizard;
  const d = frm.doc;
  const $step = $('<div class="diw-config-step flex flex-col gap-4">').appendTo($c); // frappe's wrapper: its grid stacks to one column on phones
  if (frm.is_new()) $step.append(w.section(__('What to do'), __('Pick the job; it cannot change once saved.'), w.columns(['direction'], [])));
  const needs = {
    Copy: () => w.section(__('Grains'), __('Copy From is read; Copy To receives its setup. Nothing is written until you copy.'), w.columns(['source_grain'], ['target_grain'])),
    Import: () => w.section(__('Bundle file'), __('The bundle an export produced on another site.'), frm.is_new()
      ? $('<div class="text-sm text-ink-gray-5 py-6">').text(__('Press Next to save, then attach the bundle.')) : w.columns(['bundle_file'], [])),
    Export: () => w.section(__('Recipe'), __('What the bundle carries; the records are picked next.'), w.columns(['recipe'], [])),
    Check: () => w.section(__('Grain'), __('Every part of it is read as it stands now when you save.'), w.columns(['check_grain'], [])),
  }[d.direction];
  if (needs) $step.append(needs().addClass(frm.is_new() ? 'mt-4 pt-4 border-t' : ''));
}

// Copy's Preview: what the copy carries, by part, and what it never carries.
function tatva_ss_copy_preview(frm, $c) {
  const w = frm._ss_wizard;
  if ((frm.doc.__onload || {}).write_started) {
    return $c.append(w.section(__('What the copy carried'), __('The copy has run; each record and how it landed is under Copy.'), w.stats(tatva_ss_counts(frm.doc, true))),
      tatva_ss_by_hand(frm).addClass('mt-4 pt-4 border-t'));
  }
  const $table = $('<div>');
  tatva_ss_scope(frm).then((scope) => $table.html(w.table([[__('Part'), '70%'], [__('Records'), '30%']],
    Object.entries(scope).map(([part, n]) => [frappe.utils.escape_html(__(part)), cint(n)]), __('Nothing to copy'))));
  $c.append(w.section(__('What the copy carries'), __('Counted from {0} now. Nothing is written until you copy.', [frm.doc.source_grain]), $table),
    tatva_ss_by_hand(frm).addClass('mt-4 pt-4 border-t'));
}

// The parts a copy never carries, from the server's one list, each linked to its list and its handbook page.
function tatva_ss_by_hand(frm) {
  const esc = frappe.utils.escape_html;
  frm.get_field('by_hand').$wrapper.html(`<ul>${((frm.doc.__onload || {}).by_hand || []).map((r) =>
    `<li><a href="${esc(r.route)}">${esc(r.label)}</a> · <a href="/crm-configuration/runbooks/new-grain/${esc(r.step)}" target="_blank">${esc(__('How to set it up'))}</a></li>`).join('')}</ul>`);
  return frm._ss_wizard.section(__('Not carried — set these up by hand'),
    __('A copy carries the grain\'s structure. These parts hold people, credentials and outside accounts, so they stay with you.'), frm._ss_wizard.place($('<div>'), ['by_hand']));
}

// Import's Preview: what the bundle holds, read from the file.
function tatva_ss_import_preview(frm, $c) {
  const w = frm._ss_wizard;
  $c.append($('<div class="diw-config-step">').append(w.section(__('What the bundle holds'), __('Read from the file. Nothing is written until you apply.'),
    w.columns(['recipe', 'record_count'], ['source_site', 'exported_at', 'restores']))));
}

// The verdict strip: what the run will write, or wrote; a restore counts what it puts back, and refusals show when there are any.
function tatva_ss_counts(d, done) {
  const cells = d.restores ? [[tatva_ss_put_back(d), done ? __('Put back') : __('To put back')], [d.removed_count, done ? __('Removed') : __('To remove')]]
    : [[d.created_count, done ? __('Created') : __('To create')], ...(d.direction === 'Copy' ? [] : [[d.updated_count, done ? __('Updated') : __('To update')]]),
      [d.kept_count, __('Kept')]];
  if (!d.restores && d.removed_count) cells.push([d.removed_count, done ? __('Removed') : __('To remove'), 'text-ink-red-6']);
  cells.push([d.unchanged_count, __('Unchanged')]);
  if (d.refused_count) cells.push([d.refused_count, __('Refused'), 'text-ink-red-6']);
  return cells;
}

// The stage's own reason, in frappe's alert.
function tatva_ss_error(d) {
  return d.error ? frappe.ui.alert({ theme: 'red', title: __('The run stopped'), description: d.error }) : [];
}

// The records grid, refused first, each with its reason and how it lands.
function tatva_ss_items(frm) {
  return (frm.doc.items || []).length ? frm._ss_wizard.place($('<div class="mt-4">'), ['items']) : [];
}

// Fix issues, as frappe's: the dry run starts here, its refusals show here, and the write starts from here.
function tatva_ss_fix_issues(frm, $c) {
  const w = frm._ss_wizard;
  const d = frm.doc;
  const copy = d.direction === 'Copy';
  if (d.status === 'Checking') return tatva_ss_running(frm, $c);
  if (d.status === 'Draft') {
    const $empty = frappe.ui.empty_state({
      icon: 'list-checks',
      title: copy ? __('Preview every record before copying') : __('Check every record before applying'),
      description: copy ? __('Preview saves each record onto {0} as it would land, then rolls it back. Nothing is written until you copy.', [d.target_grain])
        : __('Check saves each record as it would be, then rolls it back. Nothing is written until you apply.'),
      css_class: 'min-h-96',
    });
    if (copy) tatva_ss_scope(frm).then((scope) => $empty.append(w.stats([[Object.values(scope).reduce((n, v) => n + cint(v), 0), __('Records to carry')], [Object.keys(scope).length, __('Parts')]])));
    else $empty.append(w.stats([[d.record_count, __('Records in bundle')]]));
    return $c.append($empty);
  }
  if ((d.__onload || {}).write_started) {
    const last = w.stepper.steps[w.last].label;
    const said = { Applied: [__('No issues to fix'), __('This setup has run; what it wrote is under {0}.', [last])],
      Applying: [__('Writing now'), __('Follow it under {0}.', [last])],
      'Apply Failed': [__('Nothing was written'), __('The reason is under {0}; Retry checks again before anything is written.', [last])] }[d.status];
    const $state = frappe.ui.empty_state({ icon: 'list-checks', title: said[0], description: said[1], css_class: 'min-h-96' });
    return $c.append(d.status === 'Applied' ? $state.append(w.stats(tatva_ss_counts(d, true))) : $state);
  }
  const failed = d.status === 'Check Failed';
  const hint = failed
    ? (d.error ? __('The run stopped before every record was read. The reason is below; fix it, then Retry.')
      : copy ? __('Each blocked row names what this site lacks. Create it, then Retry.') : __('Each refused record says why under Reason. Fix them, then Retry.'))
    : copy ? __('Each row shows its name before and after, and how it lands. Copy writes them all, or none.')
      : d.restores ? __('Restore writes them all, or none.') : __('Apply writes them all, or none.');
  $c.append(w.section(failed ? __('Records refused') : d.restores ? __('Ready to restore') : copy ? __('Ready to copy') : __('Ready to apply'), hint,
    [tatva_ss_error(d), $(w.stats(tatva_ss_counts(d, false))).addClass('mb-4')]), tatva_ss_items(frm));
}

// A run in flight: the progress slot the worker's events fill, a build's wait, or a cut-off run's note (its stage is offered again).
function tatva_ss_running(frm, $c) {
  const d = frm.doc;
  if ((d.__onload || {}).cut_off) {
    return $c.append(frappe.ui.alert({ theme: 'yellow', title: __('The last run stopped before it finished.'), description: __('Start it again from the button below.') }));
  }
  if (d.status === 'Building') {
    return $c.append(frappe.ui.empty_state({ icon: 'package', title: __('Building the bundle'),
      description: __('Each record is read with everything it depends on. This page updates once it is built.'), css_class: 'min-h-96' }));
  }
  frm._ss_wizard.progress($c);
  tatva_ss_progress(frm, { setup: d.name, done: 0, total: 0 });
}

// The worker announces each record; drawn in the step's progress slot while this setup checks or applies.
function tatva_ss_progress(frm, { setup, done, total }) {
  if (!frm._ss_wizard || setup !== frm.doc.name) return;
  const copy = frm.doc.direction === 'Copy';
  const label = frm.doc.status === 'Applying' ? (copy ? __('Copying') : __('Applying')) : (copy ? __('Previewing') : __('Checking'));
  frm._ss_wizard.show_progress(label, done, total, __('records'));
}

// Copy's and Import's last step: the write's progress, then what it wrote and what is left to do.
function tatva_ss_outcome(frm, $c) {
  const w = frm._ss_wizard;
  const d = frm.doc;
  if (d.status === 'Applying') return tatva_ss_running(frm, $c);
  if (d.status === 'Apply Failed') {
    return $c.append(w.section(__('Nothing was written'), __('The reason is below or on a refused row. Fix it, then Retry: it checks again before anything is written.'),
      tatva_ss_error(d)), tatva_ss_items(frm));
  }
  const said = d.__onload || {};
  const hint = d.restores
    ? __('Restored: {0} put back, {1} removed. The {2} import applied on {3} is undone.', [tatva_ss_put_back(d), d.removed_count, d.recipe, frappe.datetime.str_to_user(d.exported_at)])
    : (d.direction === 'Copy' ? __('Copied: {0} records onto {1}.', [d.created_count, d.target_grain]) : __('Applied: {0} created, {1} updated.', [d.created_count, d.updated_count]))
      + ' ' + tatva_ss_turn_on_step(said);
  const act = (label, onclick) => frappe.ui.button({ label, variant: 'outline', size: 'sm', onclick });
  const $actions = $('<div class="flex flex-wrap items-center justify-end gap-2">').append(
    (said.switched_off || []).length ? act(__('Turn On'), () => tatva_ss_turn_on(frm)) : [],
    said.can_restore ? act(__('Restore Prior Version'), () => tatva_ss_restore(frm)) : [],
    d.direction === 'Copy' ? act(__('Check the Grain'), () => frappe.new_doc('CRM Smart Setup', { direction: 'Check', check_grain: d.target_grain })) : []);
  $c.append(w.section(d.restores ? __('Restored') : d.direction === 'Copy' ? __('Copied') : __('Applied'), hint,
    [$(w.stats(tatva_ss_counts(d, true))).addClass('mb-4'), w.place($('<div>'), ['restored_by'])], $actions), tatva_ss_items(frm));
  if (d.direction === 'Copy') $c.append(tatva_ss_by_hand(frm).addClass('mt-4 pt-4 border-t'));
}

// What is left to turn on: everything Copy or Import created arrives switched off or as a draft.
function tatva_ss_turn_on_step(said) {
  const n = (said.switched_off || []).length;
  if (!n) return '';
  return n === 1 ? __('One record arrived switched off or as a draft, shown under Lands As. Press Turn On once it is signed off.')
    : __('{0} records arrived switched off or as drafts, shown under Lands As. Press Turn On for the ones signed off.', [n]);
}

// Pick which switched-off records to turn on; each is turned on through its own form's rules, and a refusal says why on its row.
function tatva_ss_turn_on(frm) {
  const said = frm.doc.__onload || {};
  const esc = frappe.utils.escape_html;
  const dialog = new frappe.ui.Dialog({
    title: __('Turn on'),
    fields: [
      { fieldtype: 'HTML', options: `<p class="text-muted">${esc(said.can_restore
        ? __('Pick the records to switch on. Once any is on, Restore Prior Version is no longer offered.')
        : __('Pick the records to switch on.'))}</p>` },
      { fieldname: 'records', fieldtype: 'MultiCheck', label: __('Records'), reqd: 1, columns: 1,
        options: said.switched_off.map((r) => ({ label: esc(`${r.part}: ${r.record} (${__(r.arrives_as)})`), value: r.record })) },
    ],
    primary_action_label: __('Turn On'),
    primary_action({ records }) {
      frappe.call({ method: TATVA_SS_API + 'turn_on', args: { setup: frm.doc.name, records }, freeze: true }).then(({ message }) => {
        dialog.hide();
        const refused = records.length - (message || []).length;
        frappe.show_alert({ message: refused ? __('Turned on: {0}. Refused: {1}, each says why under Reason.', [(message || []).length, refused])
          : __('Turned on: {0}.', [records.length]), indicator: refused ? 'orange' : 'green' });
        frm.reload_doc();
      });
    },
  });
  dialog.show();
}

// A restore is a new import of the restore point, its check already started; the operator lands on it.
function tatva_ss_restore(frm) {
  frappe.confirm(__('Put this site back as it was before this apply? A Check runs first and nothing is written until you press Restore.'),
    () => frappe.call({ method: TATVA_SS_API + 'restore', args: { setup: frm.doc.name }, freeze: true })
      .then(({ message }) => frappe.set_route('Form', 'CRM Smart Setup', message)));
}

// Export's Records: the records grid, and every record of a product line at once when the recipe can be picked by grain.
function tatva_ss_records(frm, $c) {
  const w = frm._ss_wizard;
  const add = tatva_ss_recipe(frm).by_grain && frm.doc.status !== 'Building'
    ? frappe.ui.button({ label: __('Add All for a Product Line'), variant: 'outline', size: 'sm', onclick: () => tatva_ss_add_all(frm) }) : null;
  $c.append(w.section(__('Records to export'), __('Pick each record, or add every record of a product line at once.'), w.place($('<div>'), ['roots']), add));
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

// Export's Bundle: the build's wait, its reason when it failed, or what the built bundle holds.
function tatva_ss_bundle(frm, $c) {
  const w = frm._ss_wizard;
  const d = frm.doc;
  if (d.status === 'Building') return tatva_ss_running(frm, $c);
  if (d.status === 'Build Failed') return $c.append(w.section(__('The build stopped'), __('The reason is below. Fix it, then Retry.'), tatva_ss_error(d)));
  $c.append($('<div class="diw-config-step">').append(w.section(__('Bundle built'), __('Download it, then import it on the other site.'),
    w.columns(['record_count'], ['source_site', 'exported_at']))));
}

// Check's Checklist: counts by status, then every part with what to do about it.
function tatva_ss_checklist(frm, $c) {
  const w = frm._ss_wizard;
  const d = frm.doc;
  const n = (status) => (d.parts || []).filter((row) => row.status === status).length;
  $c.append(w.section(__('Go-live checklist for {0}', [d.check_grain]), __('Checked at {0}. Each row says what to do; press Recheck after a change.', [frappe.datetime.str_to_user(d.checked_at)]),
    [$(w.stats(Object.entries(TATVA_SS_STATUS).map(([status, colour]) => [n(status), __(status), `text-ink-${colour}-6`]))).addClass('mb-4'), w.place($('<div>'), ['parts'])]));
}

// The footer through the shell; on the step reached the action is the server's next stage, a Recheck, or the bundle's Download.
function tatva_ss_footer(frm) {
  const w = frm._ss_wizard;
  w.footer(w.step === w.reached && !frm.is_new() ? tatva_ss_action(frm) : null, (i) => tatva_ss_go(frm, i));
}

// Move to step `i` through the shell, or say what to do first.
function tatva_ss_go(frm, i) {
  if (frm._ss_wizard.go(i, tatva_ss_not_yet(frm))) tatva_ss_render(frm);
}

// The one place the Download control asks for the attachment flavour; `?` or `&` depending on what the stored url already carries.
function tatva_ss_download_url(url) {
  return `${url}${url.includes('?') ? '&' : '?'}download=1`;
}
