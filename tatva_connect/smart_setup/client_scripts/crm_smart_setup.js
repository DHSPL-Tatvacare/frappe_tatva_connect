// Desk Client Script — CRM Smart Setup, on the CRM Lead Import pattern: one action per stage, realtime progress, one reload.

const TATVA_SS_API = 'tatva_connect.smart_setup.api.';

// What each result reads as in the Records Checked grid.
const TATVA_SS_INDICATOR = { created: 'green', updated: 'blue', unchanged: 'gray', kept: 'orange', removed: 'purple', refused: 'red' };

frappe.ui.form.on('CRM Smart Setup', {
  setup(frm) {
    frappe.realtime.on('smart_setup_progress', (data) => tatva_ss_progress(frm, data));
    frappe.realtime.on('smart_setup_refresh', ({ setup }) => {
      if (setup === frm.doc.name) frm.reload_doc();
    });
    frm.set_indicator_formatter('record', (row) => TATVA_SS_INDICATOR[row.action]);
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
  if (frm.is_new()) return __('Choose Export to bundle a setup from this site, or Import to bring one in. Then save.');
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
  if (!d.bundle_file) return __('Attach the bundle file an export produced, then save.');
  if (d.restores && d.status === 'Checked') {
    return __('Ready to restore: {0} to put back, {1} to remove, {2} unchanged. Restore writes them all, or none.', [tatva_ss_put_back(d), d.removed_count, d.unchanged_count]);
  }
  if (d.restores && d.status === 'Applied') {
    return __('Restored: {0} put back, {1} removed. The {2} import applied on {3} is undone.', [tatva_ss_put_back(d), d.removed_count, d.recipe, frappe.datetime.str_to_user(d.exported_at)]);
  }
  return {
    Draft: __('Check the bundle. Every record is saved as it would be and then rolled back, so nothing changes yet.'),
    Checking: __('Checking: {0} in the bundle. Nothing is written.', [d.record_count]),
    Checked: __('Ready: {0} to create, {1} to update, {2} unchanged, {3} kept as this site has them. Apply writes them all, or none.', [d.created_count, d.updated_count, d.unchanged_count, d.kept_count]),
    'Check Failed': d.error ? __('The check stopped. The reason is under Error.')
      : __('Refused: {0}. Each refused record says why under Records Checked. Fix them, then check again.', [d.refused_count]),
    Applying: __('Applying.'),
    Applied: d.recipe === 'Workflow'
      ? __('Applied: {0} created, {1} updated. Workflows arrive as Drafts: publish them on this site.', [d.created_count, d.updated_count])
      : __('Applied: {0} created, {1} updated. The setup is live on this site.', [d.created_count, d.updated_count]),
    'Apply Failed': __('Nothing was applied. The reason is under Records Checked or Error. Fix it, then check again.'),
  }[d.status];
}

// One action per stage in place of Save, as the server names it; an edit brings Save back until it is saved.
function tatva_ss_action(frm) {
  if (frm.is_new() || frm.is_dirty()) return;
  frm.disable_save(true);
  const d = frm.doc;
  const said = d.__onload || {};
  const stage = said.next_stage;
  if (stage === 'apply') {
    frm.page.set_primary_action(d.restores ? __('Restore') : __('Apply'), () => frappe.confirm(d.restores
      ? __('To put back: {0}. To remove: {1}. Restore?', [tatva_ss_put_back(d), d.removed_count])
      : __('To create: {0}. To update: {1}. Nothing is deleted. Apply?', [d.created_count, d.updated_count]),
      () => tatva_ss_run(frm, 'apply')));
  } else if (stage) {
    frm.page.set_primary_action({ build: __('Build'), check: __('Check') }[stage], () => tatva_ss_run(frm, stage));
  } else if (d.status === 'Built') {
    frm.page.set_primary_action(__('Download'), () => window.open(d.bundle_file));
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
  const title = frm.doc.status === 'Applying' ? __('Applying') : __('Checking');
  frm.dashboard.show_progress(title, (done * 100) / total, __('{0} of {1} records', [done, total]));
}
