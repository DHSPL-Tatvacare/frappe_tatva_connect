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
  const warn = (name) => console.warn(`tatva_connect: ${name} is unavailable (public/js/tatva_connect.bundle.js did not load); the rest of this form still works.`);

  // The banner is a convenience over the URL; the webhook works whether or not it is painted.
  if (missing('tatva_render_webhook_urls')) window.tatva_render_webhook_urls = (frm) => { if (frm && frm.dashboard) frm.dashboard.clear_headline(); warn('tatva_render_webhook_urls'); };
  // A dropdown that offers nothing falls back to free text, and the server still decides what is valid.
  if (missing('tatva_set_grid_row_options')) window.tatva_set_grid_row_options = () => {};
  if (missing('tatva_set_grid_column_options')) window.tatva_set_grid_column_options = () => {};

  // LOUD -- a silent no-op here would be read as an answer, and the answer would be wrong.
  // Returning "" would let an operator save a BLANK webhook token, and a blank token is an ingress with
  // no authentication on it. Refusing is the only safe answer.
  if (missing('tatva_webhook_random_token')) {
    window.tatva_webhook_random_token = () => {
      frappe.throw({
        title: __('Cannot generate a token'),
        message: __('The shared Desk helpers did not load, so a secure token cannot be generated here. Reload the page; if it persists, the app assets need rebuilding. Do not save this account with an empty webhook token.'),
      });
    };
  }
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
