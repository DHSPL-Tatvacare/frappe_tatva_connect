// CRM Form Script (CRM Task, Form view) — fit the task modal to the viewport.
// The Quick Entry layout carries the checklist grid, which can push crm's
// DoctypeModal past the screen. That modal has no height cap / internal scroll
// and we don't fork crm, so we constrain the open dialog from here: cap height,
// scroll the body, pin the footer. Runs only for CRM Task, so it self-scopes.
// DOM-based; degrades to "no cap" if a future crm renames the dialog markup.
function setupForm({ doc }) {
  const STYLE_ID = 'tatva-task-modal-fit'
  if (!document.getElementById(STYLE_ID)) {
    const s = document.createElement('style')
    s.id = STYLE_ID
    s.textContent = [
      '.dialog-content[data-tatva-task-fit]{max-height:85vh!important;display:flex!important;flex-direction:column!important;}',
      '.dialog-content[data-tatva-task-fit]>div:first-child{flex:1 1 auto;overflow-y:auto;min-height:0;}',
      '.dialog-content[data-tatva-task-fit]>div:last-child{flex:0 0 auto;border-top:1px solid var(--outline-gray-1,#e5e7eb);}',
    ].join('\n')
    document.head.appendChild(s)
  }

  function tag() {
    // the task modal is the top-most open dialog; tag it so the CSS applies.
    const contents = document.querySelectorAll('.dialog-content')
    if (!contents.length) return
    const el = contents[contents.length - 1]
    if (el && !el.hasAttribute('data-tatva-task-fit')) {
      el.setAttribute('data-tatva-task-fit', '1')
    }
  }

  if (window.__taskModalFitObs) {
    try { window.__taskModalFitObs.disconnect() } catch (e) {}
  }
  const obs = new MutationObserver(tag)
  window.__taskModalFitObs = obs
  obs.observe(document.body, { childList: true, subtree: true })
  ;[0, 150, 400, 1000].forEach((t) => setTimeout(tag, t))
}
