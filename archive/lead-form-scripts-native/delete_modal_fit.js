// CRM Form Script (CRM Lead, Form view) — fit crm's "Delete or unlink linked documents"
// modal to the viewport. With many linked docs the panel grows past the screen and the
// whole page scrolls; we don't fork crm, so cap the panel and scroll the list, pinning
// the title and the Delete footer. Mirrors tasks/form_scripts/task_modal_fit.js.
function setupForm({ doc }) {
  const STYLE_ID = 'tatva-delete-modal-fit'
  if (!document.getElementById(STYLE_ID)) {
    const s = document.createElement('style')
    s.id = STYLE_ID
    s.textContent = [
      '[data-tatva-delete-fit]{max-height:85vh!important;display:flex!important;flex-direction:column!important;}',
      '[data-tatva-delete-fit]>div:first-child{flex:1 1 auto;min-height:0;display:flex;flex-direction:column;overflow:hidden;}',
      '[data-tatva-delete-fit]>div:first-child>div:last-child{flex:1 1 auto;min-height:0;overflow-y:auto;}',
      '[data-tatva-delete-fit]>div:last-child{flex:0 0 auto;}',
    ].join('\n')
    document.head.appendChild(s)
  }

  function tag() {
    const h3 = [...document.querySelectorAll('h3')].find((h) => /linked documents/i.test(h.textContent || ''))
    if (!h3) return
    const body = h3.closest('[class*="surface-modal"]')
    const panel = body && body.parentElement
    if (panel && !panel.hasAttribute('data-tatva-delete-fit')) panel.setAttribute('data-tatva-delete-fit', '1')
  }

  if (window.__deleteModalFitObs) {
    try { window.__deleteModalFitObs.disconnect() } catch (e) {}
  }
  const obs = new MutationObserver(tag)
  window.__deleteModalFitObs = obs
  obs.observe(document.body, { childList: true, subtree: true })
  ;[0, 150, 400, 1000].forEach((t) => setTimeout(tag, t))
}
