// CRM Form Script (CRM Lead, Form view) — Data-tab program gate.
//
// The Data tab carries BOTH the oncology PSP section (Drug Program) and the
// metabolic sections (Plan / Lab / Health Snapshot / Care & Providers). A given
// lead belongs to ONE world: an oncology drug-program lead has no metabolic plan,
// a metabolic lead has no chemo cycles. This hides the sections that don't apply,
// keyed on the lead's program (custom_current_program / custom_vertical).
//
// The decision is made server-side (tatva_connect.automation.leads.lead_section_gate),
// the SAME place the program->world mapping lives — so the JS never hardcodes a
// program list and tracks the mapping automatically. Mirrors lead_whatsapp_gate.js.
//
// Fail-safe: if the gate check errors, we SHOW everything (a wrong hide could
// strand data); the server logs the error to Error Log, so it's never silent.
// DOM-based (crm exposes no per-section hook); resilient via a MutationObserver,
// but if a future crm upgrade renames the section markup this gate degrades to
// "all sections visible" — re-verify after crm upgrades.
function setupForm({ doc, call }) {
  const HIDE_ATTR = 'data-section-gated'
  // Section labels rendered on the Data tab (must match the CRM Fields Layout labels).
  const DRUG_SECTIONS = ['Drug Program']
  const METABOLIC_SECTIONS = ['Plan', 'Lab', 'Health Snapshot', 'Care & Providers']

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

  // A Data-tab section renders its label as a heading; walk up to the section
  // container so we hide the whole block (heading + the table/fields under it).
  function sectionContainers(labels) {
    const out = []
    document.querySelectorAll('.section-label, .form-section-label, .section-head, h4, h5, .text-sm').forEach((el) => {
      const txt = (el.textContent || '').trim()
      if (!labels.includes(txt)) return
      if (el.querySelector('input, textarea, select')) return
      const container = el.closest('.form-section, .section, [data-section]') || el.parentElement
      if (container) out.push(container)
    })
    return out
  }

  function gate(isDrug) {
    // Drug-Program section: show only on a drug-program lead.
    sectionContainers(DRUG_SECTIONS).forEach((el) => setHidden(el, !isDrug))
    // Metabolic sections: hide on a drug-program lead.
    sectionContainers(METABOLIC_SECTIONS).forEach((el) => setHidden(el, isDrug))
  }

  let isDrug = false
  let resolved = false
  let scheduled = false
  function reapply() {
    if (!resolved || scheduled) return
    scheduled = true
    requestAnimationFrame(() => {
      scheduled = false
      gate(isDrug)
    })
  }

  // one observer at a time across lead navigations
  if (window.__dataTabGateObserver) {
    try {
      window.__dataTabGateObserver.disconnect()
    } catch (e) {}
  }
  const obs = new MutationObserver(reapply)
  window.__dataTabGateObserver = obs
  obs.observe(document.body, { childList: true, subtree: true })

  call('tatva_connect.automation.leads.lead_section_gate', {
    reference_name: doc.name,
  })
    .then((r) => {
      isDrug = !!(r && r.drug_program)
      resolved = true
      gate(isDrug)
    })
    .catch(() => {
      isDrug = false // fail-safe: on error, show everything (no wrong hide)
      resolved = true
      gate(false)
    })

  // re-assert shortly after load (sections can render after setup runs)
  ;[150, 500, 1200].forEach((t) => setTimeout(() => resolved && gate(isDrug), t))
}
