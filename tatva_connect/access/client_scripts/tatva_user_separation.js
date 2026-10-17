// Desk Client Script — Tatva User Separation, on the lead import's Data Import wizard: User, Assets, Review, Handover.

const TATVA_SEP_API = 'tatva_connect.access.separation.';

frappe.ui.form.on('Tatva User Separation', {
  setup(frm) {
    frappe.realtime.on('user_separation_refresh', ({ separation }) => {
      if (separation === frm.doc.name) frm.reload_doc();
    });
    $(frm.wrapper).on('dirty', () => frm._sep_wizard && tatva_sep_wizard_show(frm));
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
    tatva_sep_review(frm);
    tatva_sep_wizard(frm);
  },
});

frappe.ui.form.on('Tatva User Separation Work', {
  run(frm, cdt, cdn) {
    tatva_sep_run(frm, [cdn]);
  },
});

// Told the moment both are picked, not at save: the work cannot be reassigned to the same user.
function tatva_sep_not_same(frm) {
  if (!frm.doc.user || frm.doc.user !== frm.doc.reassign_to) return;
  frm.set_value('reassign_to', null);
  frappe.show_alert({ message: __('Reassign To must be a different user.'), indicator: 'red' });
}

// The one status line, on Handover only, and only while a run is in progress or left something to fix.
function tatva_sep_status_line(frm) {
  const d = frm.doc;
  if (frm._sep_wizard.step !== TATVA_SEP_LAST) return null;
  const left = tatva_sep_left(frm).length;
  return {
    Running: __('Running in the background. Each row updates as it finishes, and the bell rings at the end.'),
    'Completed with Errors': __('{0} rows need another look. Read each row\'s result, fix the cause, then run what is left.', [left]),
  }[d.status] || null;
}

// Each finding as frappe's own indicator line, naming its row and its fix; none is ready.
function tatva_sep_review(frm) {
  const $field = frm.get_field('review').$wrapper.empty();
  const found = (frm.doc.__onload || {}).review || [];
  (found.length ? found : [['green', __('Ready to hand over.')]]).forEach(([colour, text]) => {
    $('<p>').append($('<span>').addClass(`indicator ${colour}`).text(text)).appendTo($field);
  });
}

// Rows of both tables that still have something to run.
function tatva_sep_left(frm) {
  return [...(frm.doc.work || []), ...(frm.doc.steps || [])].filter((r) => !['Done', 'Skipped', 'Running'].includes(r.status));
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

// Each step: its label and the sections it shows; the sections carry no heading, the stepper names them.
const TATVA_SEP_STEPS = [
  [__('User'), ['user_section']],
  [__('Assets'), ['assets_section']],
  [__('Review'), ['review_section', 'cleanup_section']],
  [__('Handover'), ['assets_section', 'cleanup_section']],
];
const TATVA_SEP_LAST = TATVA_SEP_STEPS.length - 1;

function tatva_sep_wizard(frm) {
  const reached = frm.is_new() ? 0 : TATVA_SEP_LAST;
  const w = frm._sep_wizard || (frm._sep_wizard = tatva_sep_wizard_build(frm));
  if (w.docname !== frm.doc.name) w.step = null;
  w.docname = frm.doc.name;
  // A draft opens on Assets, a run on Handover, and the first save moves on to Assets; otherwise stay where the operator is.
  if (w.step == null) w.step = frm.is_new() ? 0 : frm.doc.status === 'Draft' ? 1 : TATVA_SEP_LAST;
  else if (reached > w.reached) w.step = 1;
  w.step = Math.min(w.step, reached);
  w.reached = reached;
  tatva_sep_wizard_show(frm);
}

// Frappe's own Data Import shell, as the lead import builds it, with the card at one height on every step.
function tatva_sep_wizard_build(frm) {
  const w = { step: null, reached: 0, docname: null };
  const steps = TATVA_SEP_STEPS.map(([label]) => ({ label }));
  w.stepper = new frappe.ui.Stepper({
    steps,
    label: __('Separation steps'),
    is_locked: (i) => i > w.reached,
    is_completed: (i) => i < w.step,
    on_step_click: (i) => tatva_sep_go(frm, i),
    on_locked_click: (i) => tatva_sep_go(frm, i),
  });
  w.compact = new frappe.ui.Stepper({ steps, label: __('Separation steps'), compact: true });
  frm.page.wrapper.addClass('data-import-custom-shell');
  frm.page.main.addClass('data-import-custom-page flex flex-col flex-1');
  frm.page.main.closest('.layout-main-section-wrapper').addClass('data-import-custom-wrapper');
  const $shell = $(`
    <div class="data-import-custom-ui flex justify-center w-full">
      <div class="diw-shell flex flex-col w-full min-w-0 gap-5">
        <div class="diw-stepper-wrap shrink-0 w-full"></div>
        <div class="diw-card flex flex-col w-full min-w-0 overflow-hidden rounded-lg shadow-sm border bg-surface-base" style="height: min(40rem, calc(100dvh - 12rem))">
          <div class="diw-mobile-step-header shrink-0 pt-4 px-4"></div>
          <div class="diw-step-content flex-1 min-h-0 min-w-0 w-full py-2 overflow-y-auto"></div>
          <div class="diw-status-area shrink-0 px-5 pb-4"></div>
          <div class="diw-footer flex items-center justify-between shrink-0 w-full border-t mt-auto py-4 px-5">
            <div class="diw-footer-left"></div>
            <div class="diw-footer-right flex items-center gap-2"></div>
          </div>
        </div>
      </div>
    </div>`).prependTo(frm.page.main);
  $shell.find('.diw-stepper-wrap').append(w.stepper.$el);
  $shell.find('.diw-mobile-step-header').append(w.compact.$el);
  $shell.find('.diw-step-content').append(frm.layout.wrapper);
  if (frm.footer) $shell.find('.diw-shell').append(frm.footer.wrapper); // the form's own timeline, under the card
  w.$status = $shell.find('.diw-status-area');
  w.$left = $shell.find('.diw-footer-left');
  w.$right = $shell.find('.diw-footer-right');
  return w;
}

function tatva_sep_wizard_show(frm) {
  const w = frm._sep_wizard;
  const shown = TATVA_SEP_STEPS[w.step][1];
  ['user_section', 'assets_section', 'review_section', 'cleanup_section'].forEach((s) => frm.toggle_display(s, shown.includes(s)));
  w.stepper.set_current(w.step);
  w.compact.set_current(w.step);
  const line = tatva_sep_status_line(frm);
  w.$status.empty().append(line ? $('<p class="text-muted small mb-0">').text(line) : []);
  w.$status.toggle(Boolean(line));
  w.$left.empty().append(w.step > 0 ? frappe.ui.button({ label: __('Back'), icon_left: 'arrow-left', onclick: () => tatva_sep_go(frm, w.step - 1) }) : []);
  w.$right.empty().append(tatva_sep_footer_action(frm) || []);
}

// On Handover: Save while edited, else Run all actions while anything is left; elsewhere Next.
function tatva_sep_footer_action(frm) {
  const w = frm._sep_wizard;
  if (w.step < TATVA_SEP_LAST) return frappe.ui.button({ label: __('Next'), icon_right: 'arrow-right', onclick: () => tatva_sep_next(frm) });
  if (frm.is_dirty()) return frappe.ui.button({ label: __('Save'), variant: 'solid', onclick: () => frm.save() });
  if (frm.doc.status !== 'Running' && tatva_sep_left(frm).length) return frappe.ui.button({ label: __('Run all actions'), variant: 'solid', onclick: () => tatva_sep_confirm_all(frm) });
  return null;
}

function tatva_sep_next(frm) {
  const target = frm._sep_wizard.step + 1;
  if (!frm.is_new() && !frm.is_dirty()) return tatva_sep_go(frm, target);
  return frm.save().then(() => !frm.is_dirty() && tatva_sep_go(frm, Math.min(target, frm._sep_wizard.reached)));
}

function tatva_sep_go(frm, i) {
  const w = frm._sep_wizard;
  if (i > w.reached) {
    frappe.show_alert({ message: __('Pick the user and Reassign To, then press Next.'), indicator: 'orange' });
    return;
  }
  w.step = Math.max(0, i);
  tatva_sep_wizard_show(frm);
  frappe.utils.scroll_to(frm.page.main);
}
