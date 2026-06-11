// CRM Form Script (CRM Lead, Form view) — WhatsApp 24-hour session-window gate.
//
// WhatsApp Business rule: a business may send FREE-TEXT (session) messages only
// within 24h of the customer's last inbound message. Outside that window only
// approved TEMPLATE messages are allowed. crm's WhatsApp box always shows the
// free-text input, so a rep can type a session message that WATI then rejects.
//
// This hides/disables the free-text input box when the window is CLOSED and shows
// an inline note (templates still work via the Send Template button). A new
// inbound reopens it: crm re-renders the thread on its realtime event, our
// MutationObserver fires, we re-check the window, and the box returns.
//
// Window state comes from the backend (last inbound < 24h) — there is no WATI
// flag for it; last-inbound IS Meta's definition. DOM-based (no per-component
// hook in crm), so re-verify after a crm upgrade renames the input markup.
function setupForm({ doc, call }) {
  const BANNER_ID = 'wati-window-banner'
  const RECHECK_MS = 10000 // don't hammer the backend; window only flips at 24h or on inbound

  const state = { open: null, checkedAt: 0 }

  function inputBox() {
    // The frappe-ui Textarea renders a <textarea placeholder="Type your message here...">.
    return document.querySelector('textarea[placeholder^="Type your message"]')
  }

  function inputRow(ta) {
    // The bar holding attachments + textarea + send button.
    return (ta && (ta.closest('div.flex.items-end') || ta.parentElement?.parentElement)) || null
  }

  function setBanner(row, on) {
    let el = document.getElementById(BANNER_ID)
    if (on) {
      if (!el && row && row.parentElement) {
        el = document.createElement('div')
        el.id = BANNER_ID
        el.textContent =
          '24-hour window closed — you can only send template messages until the patient replies.'
        el.style.cssText =
          'margin:6px 12px;padding:8px 12px;border-radius:8px;font-size:13px;' +
          'background:var(--surface-amber-1,#fef3c7);color:var(--ink-amber-3,#92400e);' +
          'border:1px solid var(--outline-amber-2,#fcd34d);'
        row.parentElement.insertBefore(el, row)
      }
    } else if (el) {
      el.remove()
    }
  }

  function applyDom() {
    const ta = inputBox()
    if (!ta) return // WhatsApp tab not open / box not rendered
    const row = inputRow(ta)
    if (state.open === false) {
      ta.disabled = true // robust guard even if row detection misses
      ta.style.opacity = '0.5'
      if (row) row.style.display = 'none'
      setBanner(row, true)
    } else if (state.open === true) {
      ta.disabled = false
      ta.style.opacity = ''
      if (row) row.style.display = ''
      setBanner(row, false)
    }
    // state.open === null: unknown yet -> leave the box as crm rendered it
  }

  function recheck() {
    const now = Date.now()
    if (now - state.checkedAt < RECHECK_MS) return
    state.checkedAt = now
    call('tatva_connect.api.whatsapp.whatsapp_window_state', {
      reference_doctype: doc.doctype,
      reference_name: doc.name,
    })
      .then((r) => {
        state.open = !!(r && r.open)
        applyDom()
      })
      .catch(() => {
        // fail-open: on error don't block the rep (a closed-window send just fails at WATI)
        state.open = true
        applyDom()
      })
  }

  let scheduled = false
  function tick() {
    if (scheduled) return
    scheduled = true
    requestAnimationFrame(() => {
      scheduled = false
      if (inputBox()) {
        recheck() // throttled; catches inbound (DOM re-render) + first render
        applyDom() // cheap; reflects cached state every time
      }
    })
  }

  // one observer at a time across lead navigations
  if (window.__watiWindowObserver) {
    try {
      window.__watiWindowObserver.disconnect()
    } catch (e) {}
  }
  const obs = new MutationObserver(tick)
  window.__watiWindowObserver = obs
  obs.observe(document.body, { childList: true, subtree: true })

  recheck()
  ;[300, 900, 1800].forEach((t) => setTimeout(tick, t))
}
