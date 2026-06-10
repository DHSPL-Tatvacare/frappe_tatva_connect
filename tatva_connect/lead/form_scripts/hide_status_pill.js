// CRM Form Script (CRM Lead, Form view) — hide the native CRM Lead Status pill.
//
// crm renders a top-right status dropdown ("New / Contacted / …") on every lead.
// Niva (and other programs) drive lifecycle through custom_stage / custom_substage
// instead, so the native pill is misleading. There's no crm hook to suppress it,
// so we hide its DOM node — the status button whose label is one of the known
// CRM Lead Status values. Same capture-resilient pattern as lead_whatsapp_gate.js:
// a single MutationObserver (re-armed across lead navigations) plus a few timed
// re-asserts, since the header renders after setup runs.
//
// Fail-safe: if we can't find the node, nothing is hidden (the pill just stays) —
// never throws. DOM-based; if a future crm upgrade renames the markup this gate
// degrades to "pill visible" — re-verify after crm upgrades.
function setupForm({ doc }) {
  const HIDE_ATTR = 'data-stage-pill-hidden'
  // The 7 native CRM Lead Status values (see MEMORY: CRM Lead Status lookup).
  const STATUS_LABELS = [
    'New',
    'Contacted',
    'Nurture',
    'Qualified',
    'Converted',
    'Unqualified',
    'Junk',
  ]

  function setHidden(el) {
    if (!el || el.getAttribute(HIDE_ATTR)) return
    el.style.display = 'none'
    el.setAttribute(HIDE_ATTR, '1')
  }

  function hidePill() {
    // The status pill is a button/dropdown trigger whose trimmed text is exactly one
    // of the status labels and which is NOT an input (mirrors byExactText in the gate
    // script). It lives in the form header, so we match on the button itself.
    document.querySelectorAll('button, [role="button"], .dropdown-toggle').forEach((el) => {
      const txt = (el.textContent || '').trim()
      if (
        STATUS_LABELS.includes(txt) &&
        !el.querySelector('input, textarea, select')
      ) {
        setHidden(el)
      }
    })
  }

  let scheduled = false
  function reapply() {
    if (scheduled) return
    scheduled = true
    requestAnimationFrame(() => {
      scheduled = false
      hidePill()
    })
  }

  // one observer at a time across lead navigations
  if (window.__stagePillObserver) {
    try {
      window.__stagePillObserver.disconnect()
    } catch (e) {}
  }
  const obs = new MutationObserver(reapply)
  window.__stagePillObserver = obs
  obs.observe(document.body, { childList: true, subtree: true })

  hidePill()
  // re-assert shortly after load (header can render after setup runs)
  ;[150, 500, 1200].forEach((t) => setTimeout(hidePill, t))
}
