// Desk Client Script — Tatva User Separation, on the shared Data Import wizard shell: Who, Plan, Check, Handover.

const TATVA_SEP_API = 'tatva_connect.access.separation.';

frappe.ui.form.on('Tatva User Separation', {
  setup(frm) {
    frappe.realtime.on('user_separation_refresh', ({ separation }) => {
      if (separation === frm.doc.name) frm.reload_doc();
    });
    $(frm.wrapper).on('dirty', () => frm._sep_wizard && tatva_sep_footer(frm));
  },

  onload(frm) {
    const people = () => ({ filters: { enabled: 1, user_type: 'System User' } });
    frm.set_query('user', people);
    frm.set_query('reassign_to', people);
    frm.set_query('assign_to', 'work', (doc, cdt, cdn) => {
      const row = locals[cdt][cdn];
      return { query: TATVA_SEP_API + 'assignee_query', filters: { ref_doctype: row.ref_doctype, vertical: row.vertical, crm_group: row.crm_group, program: row.program } };
    });
  },

  user(frm) {
    tatva_sep_not_same(frm);
  },

  reassign_to(frm) {
    tatva_sep_not_same(frm);
  },

  refresh(frm) {
    frm.set_df_property('work', 'cannot_add_rows', 1); // a work row only comes from the plan or a Split, so the grid offers no blank Add row
    if (tatva_sep_running(frm)) frm.disable_form(); // the run owns the record until it ends (server: _hold_while_running)
    tatva_sep_wizard(frm);
    tatva_history_button(frm);
  },
});

frappe.ui.form.on('Tatva User Separation Work', {
  run(frm, cdt, cdn) {
    tatva_sep_run(frm, [cdn]);
  },

  // A new row of the same group, for a second person; the save deals the group's records out evenly.
  split(frm, cdt, cdn) {
    const row = locals[cdt][cdn];
    frm.add_child('work', { ref_doctype: row.ref_doctype, grain: row.grain, vertical: row.vertical, crm_group: row.crm_group, program: row.program });
    frm.refresh_field('work');
    frappe.show_alert({ message: __('Pick who takes the new share, then Save. The records are shared out evenly.'), indicator: 'blue' });
  },
});

// Told the moment both are picked, not at save: the work cannot be reassigned to the same user.
function tatva_sep_not_same(frm) {
  if (!frm.doc.user || frm.doc.user !== frm.doc.reassign_to) return;
  frm.set_value('reassign_to', null);
  frappe.show_alert({ message: __('Reassign To must be a different user.'), indicator: 'red' });
}

// A run still held by its job; one whose job is gone (server: stranded) reads as ended, so Retry picks it up.
function tatva_sep_running(frm) {
  return frm.doc.status === 'Running' && !frm.doc.__onload?.stranded;
}

// Rows of both tables that still have something to run.
function tatva_sep_left(frm) {
  const settled = tatva_sep_running(frm) ? ['Done', 'Skipped', 'Running'] : ['Done', 'Skipped'];
  return [...(frm.doc.work || []), ...(frm.doc.steps || [])].filter((r) => !settled.includes(r.status));
}

// Save first when edited, then queue the picked rows, or every row not yet done.
function tatva_sep_run(frm, rows) {
  const go = () => frappe.call({ method: TATVA_SEP_API + 'run', args: { separation: frm.doc.name, rows }, freeze: true })
    .then(() => frm.reload_doc());
  if (frm.is_dirty()) return frm.save().then(() => !frm.is_dirty() && go());
  go();
}

function tatva_sep_confirm_all(frm) {
  frappe.confirm(
    __('Hand over the work of {0}? Each row moves to its Assign To, then the cleanup runs and the account is disabled last.', [frm.doc.full_name || frm.doc.user]),
    () => tatva_sep_run(frm),
  );
}

// The server's review, split into what blocks the handover and what the operator should know.
function tatva_sep_findings(frm) {
  const found = (frm.doc.__onload || {}).review || [];
  return { blockers: found.filter(([level]) => level === 'red'), notes: found.filter(([level]) => level !== 'red') };
}

const TATVA_SEP_STEPS = [__('Who'), __('Plan'), __('Check'), __('Handover')];
const TATVA_SEP_LAST = TATVA_SEP_STEPS.length - 1;
const TATVA_SEP_FIELDS = ['user', 'full_name', 'reassign_to', 'work', 'steps'];

// Has a handover started: the separation left Draft, or any row has been run.
function tatva_sep_ran(frm) {
  return frm.doc.status !== 'Draft' || [...(frm.doc.work || []), ...(frm.doc.steps || [])].some((r) => r.status && r.status !== 'Pending');
}

// The furthest step unlocked: Check once saved, Handover once it runs.
function tatva_sep_reached(frm) {
  if (frm.is_new()) return 0;
  return tatva_sep_ran(frm) ? TATVA_SEP_LAST : 2;
}

// The step the form opens on: a new one on Who, a planned one on Plan, a run on Handover.
function tatva_sep_landing(frm) {
  const reached = tatva_sep_reached(frm);
  return reached === 2 ? 1 : reached;
}

// Built once per form; the shell lands, follows and stays by this separation's own reached and landing steps.
function tatva_sep_wizard(frm) {
  const w = frm._sep_wizard || (frm._sep_wizard = tatva_sep_wizard_build(frm));
  if (!w) return;
  w.sync(tatva_sep_reached(frm), tatva_sep_landing(frm));
  tatva_sep_render(frm);
}

// The shared Data Import shell over this separation's own steps; the last is done once the handover completed.
function tatva_sep_wizard_build(frm) {
  return tatva_wizard_shell(frm, {
    steps: TATVA_SEP_STEPS,
    label: __('Separation steps'),
    fields: TATVA_SEP_FIELDS,
    finished: () => frm.doc.status === 'Completed',
    on_step_click: (i) => tatva_sep_go(frm, i),
  });
}

// Redraw the current step through the shell, then the footer.
function tatva_sep_render(frm) {
  const w = frm._sep_wizard;
  w.render(w.step, ($c) => [tatva_sep_who, tatva_sep_plan, tatva_sep_check, tatva_sep_handover][w.step](frm, $c));
  tatva_sep_footer(frm);
}

// Who: the person leaving on the left, the default receiver of their work on the right.
function tatva_sep_who(frm, $c) {
  const w = frm._sep_wizard;
  $c.append($('<div class="diw-config-step flex flex-col gap-4">').append( // frappe's wrapper: its grid stacks to one column on phones
    w.section(__('Who is leaving'), __('Their open work moves to Reassign To unless a row says otherwise.'), w.columns(['user', 'full_name'], ['reassign_to']))));
}

// A shell section holding one table field, or a line saying it is empty.
function tatva_sep_section(frm, title, hint, field, nothing) {
  const w = frm._sep_wizard;
  return w.section(title, hint, (frm.doc[field] || []).length ? w.place($('<div>'), [field]) : $('<p class="text-sm text-ink-gray-5">').text(nothing));
}

// Plan: everything the person holds, the open work and who takes it, then the cleanup; and what is never touched.
function tatva_sep_plan(frm, $c) {
  $c.append(
    tatva_sep_section(frm, __('Open work to hand over'), __('Each row moves to its Assign To. Split a row to share its records evenly across more people.'), 'work', __('Nothing open to hand over.')),
    tatva_sep_section(frm, __('Cleanup'), __('Runs once every work row has moved; the account is disabled last.'), 'steps', __('Nothing to clean up.')).addClass('mt-4 pt-4 border-t'),
    $('<p class="text-xs text-ink-gray-5 mt-4">').text(__('Never touched: history, calls, messages, logs, and who created or changed any record.')));
}

// What the handover will move: records still to go, the people receiving them, the cleanup steps.
function tatva_sep_totals(frm) {
  const open = (frm.doc.work || []).filter((r) => r.status !== 'Done');
  return [
    [open.reduce((n, r) => n + cint(r.records), 0), __('Records to move')],
    [new Set(open.map((r) => r.assign_to).filter(Boolean)).size, __('People receiving')],
    [(frm.doc.steps || []).length, __('Cleanup steps')],
  ];
}

// Check, as frappe's Fix issues: blockers and heads-ups with their fixes, or ready to hand over.
function tatva_sep_check(frm, $c) {
  const w = frm._sep_wizard;
  const { blockers, notes } = tatva_sep_findings(frm);
  if (!blockers.length && !notes.length) {
    return $c.append(frappe.ui.empty_state({
      icon: 'list-checks',
      title: __('Ready to hand over'),
      description: __('Each work row moves to its new owner, then the cleanup runs and the account is disabled last.'),
      css_class: 'min-h-96',
    }).append(w.stats(tatva_sep_totals(frm))));
  }
  const findings = [...blockers, ...notes];
  const badge = ([level]) => frappe.ui.badge.html({ label: level === 'red' ? __('Blocker') : __('Heads-up'), theme: level === 'red' ? 'red' : 'orange' });
  const fix = ([, , to], i) => (to ? frappe.ui.button.html({ label: __('Fix'), variant: 'ghost', size: 'xs', icon_right: 'arrow-up-right', attrs: { 'data-fix': i } }) : '');
  const $table = $(w.table([[__('Level'), '14%'], [__('What to do'), '72%'], ['', '14%']],
    findings.map((f, i) => [badge(f), frappe.utils.escape_html(f[1]), fix(f, i)]), __('Nothing to fix')));
  $table.on('click', '[data-fix]', (e) => tatva_sep_fix(frm, findings[cint($(e.currentTarget).attr('data-fix'))][2]));
  $c.append(
    w.head(blockers.length ? __('Fix these before handing over') : __('Ready, with heads-ups')),
    $(w.stats([[blockers.length, __('Blockers'), blockers.length ? 'text-ink-red-6' : ''], [notes.length, __('Heads-ups')], tatva_sep_totals(frm)[0]])).addClass('mb-4'),
    $table);
}

// Goes where a finding is put right: its work row on Plan, opened in frappe's own row form; or the record or list that needs it.
function tatva_sep_fix(frm, to) {
  if (to.row) {
    tatva_sep_go(frm, 1);
    frm.fields_dict.work.grid.grid_rows[to.row - 1]?.toggle_view(true);
  } else if (to.form) {
    frappe.set_route('Form', ...to.form);
  } else if (to.list) {
    frappe.route_options = to.list[1];
    frappe.set_route('List', to.list[0]);
  }
}

// Handover: progress while it runs, its outcome after, and every row's result.
function tatva_sep_handover(frm, $c) {
  const w = frm._sep_wizard;
  const rows = [...(frm.doc.work || []), ...(frm.doc.steps || [])];
  const done = rows.filter((r) => ['Done', 'Skipped'].includes(r.status)).length;
  if (tatva_sep_running(frm)) {
    w.progress($c);
    w.show_progress(__('Handing over'), done, rows.length, __('rows'));
  } else {
    const failed = rows.filter((r) => ['Failed', 'Partly done'].includes(r.status)).length;
    const disabled = (frm.doc.steps || []).some((s) => s.step === 'Disable account' && s.status === 'Done');
    $c.append($(w.stats([[`${done} / ${rows.length}`, __('Rows done')], [failed, __('Need another look'), failed ? 'text-ink-red-6' : ''],
      [disabled ? __('Disabled') : __('Active'), __('Account')]])).addClass('mb-4'));
  }
  $c.append(
    tatva_sep_section(frm, __('Work'), __('Each row shows what moved and what was refused.'), 'work', __('Nothing open to hand over.')).addClass('mt-4'),
    tatva_sep_section(frm, __('Cleanup'), __('Runs once every work row has moved; the account is disabled last.'), 'steps', __('Nothing to clean up.')).addClass('mt-4 pt-4 border-t'));
}

// The footer through the shell: Hand over on Check once it is reached, Retry on Handover for what is left; both held while a blocker stands, as run() refuses then.
function tatva_sep_footer(frm) {
  const w = frm._sep_wizard;
  const blocked = tatva_sep_findings(frm).blockers.length > 0;
  const hold = { disabled: blocked, title: blocked ? __('Fix the blockers first') : '' };
  let action = null;
  if (w.step === 2 && w.reached === 2) action = { label: __('Hand over'), run: () => tatva_sep_confirm_all(frm), ...hold };
  else if (w.step === 3 && !tatva_sep_running(frm) && tatva_sep_left(frm).length) action = { label: __('Retry'), icon: 'refresh-cw', run: () => tatva_sep_confirm_all(frm), ...hold };
  w.footer(action, (i) => tatva_sep_go(frm, i));
}

// Move to step `i` through the shell, or say what to do first.
function tatva_sep_go(frm, i) {
  const not_yet = frm._sep_wizard.reached ? __('Hand over from Check; this step opens once the handover runs.') : __('Pick the user and Reassign To, then press Next.');
  if (frm._sep_wizard.go(i, not_yet)) tatva_sep_render(frm);
}
