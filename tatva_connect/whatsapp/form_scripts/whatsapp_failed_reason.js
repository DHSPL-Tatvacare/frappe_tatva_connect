// CRM Form Script (CRM Lead, Form view) — shipped as a CRM Form Script fixture.
// Shows WATI's delivery-failure reason on hover of a failed WhatsApp bubble, using the
// NATIVE HTML `title` attribute (browser tooltip — no custom UI, no dialog). crm renders
// each bubble with id = the WhatsApp Message name, so we map {name: reason} straight onto it.
// No crm code touched.
function setupForm({ doc, call }) {
  let reasons = {}
  let lastLoad = 0

  function apply() {
    Object.keys(reasons).forEach((name) => {
      const el = document.getElementById(name)
      if (el && el.title !== reasons[name]) el.title = reasons[name]
    })
  }

  async function load() {
    try {
      reasons = (await call('tatva_connect.api.whatsapp.failed_reasons', {
        reference_doctype: doc.doctype, reference_name: doc.name,
      })) || {}
    } catch (e) {
      return
    }
    apply()
  }

  // As the thread (re)renders, re-apply titles (cheap); throttle the refetch so newly
  // failed messages (e.g. after Refresh WhatsApp) pick up their reason without spamming.
  function tick() {
    apply()
    const now = (typeof performance !== 'undefined' && performance.now) ? performance.now() : 0
    if (now - lastLoad > 5000) { lastLoad = now; load() }
  }
  if (window.__watiFailObs) {
    try { window.__watiFailObs.disconnect() } catch (e) {}
  }
  const obs = new MutationObserver(tick)
  window.__watiFailObs = obs
  obs.observe(document.body, { childList: true, subtree: true })

  load()
}
