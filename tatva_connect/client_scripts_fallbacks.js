// Prepended to EVERY seeded Desk Client Script by client_scripts_seed.seed(). Read the "why" there.
// The shared helpers live in the content-hashed public/js/tatva_connect.bundle.js. When that asset is
// missing the helpers are undefined, and since each form calls one on the FIRST line of `refresh`, the
// whole handler dies on a ReferenceError -- taking the secret toggles, the buttons and the banner with
// it and leaving a form that looks finished and does nothing.
// So each helper gets a fallback here, defined ONLY when the real one did not arrive. Two of them must
// NOT go quiet, and the split is the point: a helper whose absence merely costs an affordance no-ops, a
// helper whose absence would let an operator draw a WRONG CONCLUSION says so.

(() => {
  // The bundle wins whenever it loaded; nothing below ever shadows a real helper.
  const missing = (name) => typeof window[name] !== 'function';

  // LOUD -- without the menu no token can be generated or copied, so say why instead of showing a bare form.
  if (missing('tatva_webhook_actions')) {
    window.tatva_webhook_actions = (frm) => {
      frm.dashboard.clear_headline();
      frm.dashboard.set_headline(__('The shared Desk helpers did not load, so the webhook token and URLs cannot be shown here. Reload the page; if it persists, the app assets need rebuilding.'));
    };
  }
  // The banner is a convenience over the URL; the webhook works whether or not it is painted.
  if (missing('tatva_render_webhook_urls')) window.tatva_render_webhook_urls = () => {};
  // A dropdown that offers nothing falls back to free text, and the server still decides what is valid.
  if (missing('tatva_set_grid_row_options')) window.tatva_set_grid_row_options = () => {};
  if (missing('tatva_set_grid_column_options')) window.tatva_set_grid_column_options = () => {};

  // LOUD -- a stepped form without its shell has no steps and no footer actions, so say why instead.
  if (missing('tatva_wizard_shell')) {
    window.tatva_wizard_shell = (frm) => {
      frm.dashboard.clear_headline();
      frm.dashboard.set_headline(__('The shared Desk helpers did not load, so this form cannot show its steps. Reload the page; if it persists, the app assets need rebuilding.'));
      return null;
    };
  }
  // The Version list is still reachable from the list view; the button is a shortcut.
  if (missing('tatva_history_button')) window.tatva_history_button = () => {};
  // A pill is decoration; without the bundle the word itself still reads.
  if (missing('tatva_status_pill')) window.tatva_status_pill = (word) => word || '';
  // LOUD -- a button that asks the provider and paints nothing reads as "the provider has nothing".
  if (missing('tatva_pick_rows')) {
    window.tatva_pick_rows = (opts) => {
      frappe.msgprint({
        title: opts && opts.title,
        indicator: 'orange',
        message: __('Desk helpers did not load, so this cannot show what the provider reports. Reload the page and try again.'),
      });
      return null;
    };
  }
  // LOUD -- running discovery from a prompt that failed to load is running it unexplained.
  if (missing('tatva_fb_discovery_prompt') || missing('tatva_fb_discover')) {
    window.tatva_fb_discovery_prompt = () => '';
    window.tatva_fb_discover = () => {
      frappe.msgprint({
        title: __('Cannot refresh from Facebook'),
        indicator: 'orange',
        message: __('Desk helpers did not load, so this cannot say what it will do. Reload the page and try again.'),
      });
    };
  }

  // LOUD -- without the app picker an action that reads Meta does nothing, so say why.
  if (missing('tatva_fb_app_dialog')) {
    window.tatva_fb_app_dialog = () => frappe.msgprint({
      title: __('Cannot ask Meta'),
      indicator: 'orange',
      message: __('Desk helpers did not load, so the Facebook App cannot be picked. Reload the page and try again.'),
    });
  }

  // LOUD -- a first sync can fetch every lead a form ever collected; an empty confirm asks nothing.
  if (missing('tatva_sync_now_prompt')) {
    window.tatva_sync_now_prompt = () =>
      __('Desk helpers did not load. A source that has never run fetches every lead the form has collected. Reload the page before continuing.');
  }

  // A missing Merge button only costs the shortcut; Desk's own Rename with "Merge with existing" still works.
  if (missing('tatva_merge_button')) window.tatva_merge_button = () => {};

  // A validate button that renders nothing reads as "it passed". Say that nothing was checked.
  if (missing('tatva_show_check_report')) {
    window.tatva_show_check_report = () => {
      frappe.msgprint({
        title: __('Result could not be shown'),
        indicator: 'orange',
        message: __('The check ran, but the shared Desk helpers did not load so its result cannot be displayed. Reload the page and try again — do not read this as a pass.'),
      });
    };
  }
})();
