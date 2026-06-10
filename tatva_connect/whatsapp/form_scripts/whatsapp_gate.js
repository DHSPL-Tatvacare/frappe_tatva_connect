// CRM Form Script (CRM Lead, Form view) — WhatsApp UI gate.
//
// crm shows the WhatsApp tab/buttons on EVERY lead (the `whatsappEnabled` flag is
// global, not lead-aware). This hides the WhatsApp entry points on a lead that has
// NO WATI route. The decision comes from the SAME routing rules used to send
// (tatva_connect.whatsapp.routing.lead_has_route -> resolve_account_for_lead), so it
// tracks CRM WATI Account Routing automatically — no hardcoded group here.
//
// Fail-safe: if the route check errors, we HIDE (consistent with "no route = send
// blocked"); the server logs the error to Error Log, so it's never silent.
// No crm code touched. DOM-based (crm exposes no per-lead tab hook); resilient via
// a MutationObserver, but if a future crm upgrade renames the markup this gate
// degrades to "tab visible" — re-verify after crm upgrades.
function setupForm({ doc, call }) {
  const HIDE_ATTR = 'data-wati-gated'
  const TAB_TEXT = 'WhatsApp' // the tab label + the WhatsApp header title
  const ITEM_TEXT = 'WhatsApp Message' // the entry in the "New" dropdown

  function setHidden(el, hidden) {
    if (!el) return
    if (hidden) {
      el.style.display = 'none'
      el.setAttribute(HIDE_ATTR, '1')
    } else if (el.getAttribute(HIDE_ATTR)) {
      el.style.display = ''
      el.removeAttribute(HIDE_ATTR)
    }
  }

  function byExactText(text, selector) {
    const out = []
    document.querySelectorAll(selector).forEach((el) => {
      if ((el.textContent || '').trim() === text && !el.querySelector('input, textarea, select')) {
        out.push(el)
      }
    })
    return out
  }

  function gate(hasRoute) {
    // entry point 1: the WhatsApp tab in the tab strip (+ the tab's header title)
    byExactText(TAB_TEXT, '[role="tab"], button, a, .tab').forEach((el) => setHidden(el, !hasRoute))
    // entry point 2: the "WhatsApp Message" item in the "New" dropdown (rendered on open)
    byExactText(ITEM_TEXT, 'button, a, [role="menuitem"], .dropdown-item').forEach((el) =>
      setHidden(el, !hasRoute),
    )
  }

  let hasRoute = false
  let resolved = false
  let scheduled = false
  function reapply() {
    if (!resolved || scheduled) return
    scheduled = true
    requestAnimationFrame(() => {
      scheduled = false
      gate(hasRoute)
    })
  }

  // one observer at a time across lead navigations
  if (window.__watiGateObserver) {
    try {
      window.__watiGateObserver.disconnect()
    } catch (e) {}
  }
  const obs = new MutationObserver(reapply)
  window.__watiGateObserver = obs
  obs.observe(document.body, { childList: true, subtree: true })

  call('tatva_connect.whatsapp.routing.lead_has_route', {
    reference_doctype: doc.doctype,
    reference_name: doc.name,
  })
    .then((r) => {
      hasRoute = !!(r && r.has_route)
      resolved = true
      gate(hasRoute)
    })
    .catch(() => {
      hasRoute = false // fail-safe: hide WhatsApp when we can't confirm a route
      resolved = true
      gate(false)
    })

  // re-assert shortly after load (tab strip can render after setup runs)
  ;[150, 500, 1200].forEach((t) => setTimeout(() => resolved && gate(hasRoute), t))
}
