// CRM Form Script (CRM Lead, Form view) — shipped as a CRM Form Script fixture.
// Hooks the built-in "Send Template" button (capture-phase click interceptor) and
// opens ONE unified, theme-aware Send-Template dialog (pick → preview → fill → send,
// all in a single box). Styling uses frappe-ui design tokens (--surface/--ink/--outline)
// so it follows light/dark theme. ONE combo widget (input + floating menu) is used for BOTH
// the template picker and the variable pickers — identical UX. The menu floats (absolute,
// overlays content → no height jump) and our dialog's content overflow is scoped to visible
// so the list can spill past the box (never clipped). No crm code touched, no hardcoded colors.
function setupForm({ doc, $dialog, call, createToast }) {
  const BTN_TEXT = 'Send Template'
  const REFRESH_LABEL = 'Refresh WhatsApp'
  const DIALOG_TITLE = 'Send WhatsApp Template'

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
  }
  const notify = (msg, ok) => createToast({ message: msg, type: ok ? 'success' : 'error' })

  const iconSpin =
    '<svg class="wati-spin" width="13" height="13" viewBox="0 0 24 24" fill="none" ' +
    'xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="12" r="10" stroke="currentColor" ' +
    'stroke-width="4" opacity="0.25"></circle><path fill="currentColor" opacity="0.75" ' +
    'd="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg>'
  const iconRefresh =
    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px">' +
    '<path d="M21 12a9 9 0 1 1-2.6-6.36"/><path d="M21 3v6h-6"/></svg>'
  const iconCaret =
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>'

  // One-time styles: spinner keyframe + dialog theming via design tokens (dark-aware).
  if (!document.getElementById('wati-tpl-style')) {
    const st = document.createElement('style')
    st.id = 'wati-tpl-style'
    st.textContent =
      '@keyframes wati-spin{to{transform:rotate(360deg)}}' +
      '.wati-spin{animation:wati-spin .7s linear infinite;vertical-align:-2px}' +
      // let THIS dialog's content show the floating menu past its edge (no clip).
      '.dialog-overlay[data-dialog="' + DIALOG_TITLE + '"] .dialog-content{overflow:visible}' +
      '.wati-row{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:12px}' +
      '.wati-meta{font-size:12px;color:var(--ink-gray-5);line-height:1.7}' +
      '.wati-meta b{color:var(--ink-gray-8);font-weight:600}' +
      '.wati-iconbtn{display:inline-flex;align-items:center;gap:6px;flex:0 0 auto;font-size:12px;' +
      'font-weight:500;color:var(--ink-gray-6);background:var(--surface-gray-2);' +
      'border:1px solid var(--outline-gray-2);border-radius:7px;padding:5px 10px;cursor:pointer;white-space:nowrap}' +
      '.wati-iconbtn:hover{background:var(--surface-gray-3)}' +
      '.wati-field{width:100%;box-sizing:border-box;border:1px solid var(--outline-gray-2);border-radius:8px;' +
      'padding:8px 11px;font-size:14px;background:var(--surface-white);color:var(--ink-gray-8);outline:none}' +
      '.wati-field:focus{border-color:var(--outline-gray-3)}' +
      '.wati-label{font-size:13px;font-weight:600;color:var(--ink-gray-8);margin:16px 0 5px}' +
      '.wati-label .h{color:var(--ink-gray-5);font-weight:400}' +
      '.wati-preview{white-space:pre-wrap;background:var(--surface-gray-2);border:1px solid var(--outline-gray-2);' +
      'border-radius:8px;padding:12px;font-size:13px;color:var(--ink-gray-7);max-height:200px;overflow:auto;margin-top:12px}' +
      '.wati-chip{background:var(--surface-gray-4);color:var(--ink-gray-8);border-radius:4px;padding:0 5px;font-weight:600}' +
      '.wati-empty{font-size:13px;color:var(--ink-gray-6);line-height:1.6;margin-top:10px}' +
      // combo: input head + floating menu (absolute → overlays, no layout jump)
      '.wati-dd{position:relative}' +
      '.wati-ddhead{position:relative}' +
      '.wati-ddhead .wati-field{padding-right:32px}' +
      '.wati-caret{position:absolute;right:11px;top:50%;transform:translateY(-50%);pointer-events:none;' +
      'color:var(--ink-gray-5);display:inline-flex}' +
      '.wati-menu{position:absolute;left:0;right:0;top:calc(100% + 6px);z-index:30;background:var(--surface-white);' +
      'border:1px solid var(--outline-gray-2);border-radius:8px;box-shadow:0 8px 28px rgba(0,0,0,.16);' +
      'max-height:264px;overflow:auto;padding:4px}' +
      '.wati-opt{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 10px;' +
      'border-radius:6px;font-size:13px;color:var(--ink-gray-8);cursor:pointer}' +
      '.wati-opt:hover{background:var(--surface-gray-3)}' +
      '.wati-opt>span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '.wati-opt .v{flex:0 0 auto;font-size:11px;color:var(--ink-gray-4)}' +
      '.wati-noopt{padding:9px 10px;font-size:13px;color:var(--ink-gray-4)}'
    document.head.appendChild(st)
  }

  // Run cb once the element (by id) exists in the (teleported) dialog DOM.
  function whenReady(id, cb, tries) {
    const el = document.getElementById(id)
    if (el) return cb(el)
    if ((tries || 0) > 25) return
    setTimeout(() => whenReady(id, cb, (tries || 0) + 1), 40)
  }

  // ---- ONE reusable combo: input + its floating menu sibling. ----
  // getOptions(query) → flat [{label, value, sub}]; onSelect(opt) fires on pick.
  function combo(input, menu, getOptions, onSelect) {
    function render(q) {
      const opts = getOptions(q) || []
      menu._opts = opts
      menu.innerHTML = opts.length
        ? opts.map((o, i) =>
            '<div class="wati-opt" data-i="' + i + '"><span>' + esc(o.label) + '</span>' +
            (o.sub ? '<span class="v">' + esc(o.sub) + '</span>' : '') + '</div>').join('')
        : '<div class="wati-noopt">No matches</div>'
    }
    const open = (q) => { render(q); menu.style.display = '' }
    const close = () => { menu.style.display = 'none' }
    input.addEventListener('focus', () => open(''))
    input.addEventListener('click', () => open(''))
    input.addEventListener('input', () => open(input.value))
    // mousedown (before blur) + preventDefault keeps focus so the pick registers.
    menu.addEventListener('mousedown', (e) => {
      const opt = e.target.closest ? e.target.closest('.wati-opt') : null
      if (!opt) return
      e.preventDefault()
      const o = menu._opts && menu._opts[+opt.getAttribute('data-i')]
      if (o) { onSelect(o); close() }
    })
    input.addEventListener('blur', () => setTimeout(close, 120))
  }

  // ---- Refresh WhatsApp (header action) ----
  function refreshButton() {
    return Array.from(document.querySelectorAll('button')).find(
      (b) => (b.textContent || '').trim().replace(/^⟳\s*/, '') === REFRESH_LABEL,
    )
  }
  function setBtnBusy(busy) {
    const btn = refreshButton()
    if (!btn) return
    if (busy) {
      if (!btn.dataset.watiOrig) btn.dataset.watiOrig = btn.innerHTML
      btn.innerHTML = iconSpin + '&nbsp;' + REFRESH_LABEL
      btn.style.opacity = '0.7'
      btn.style.pointerEvents = 'none'
    } else if (btn.dataset.watiOrig) {
      btn.innerHTML = btn.dataset.watiOrig
      delete btn.dataset.watiOrig
      btn.style.opacity = ''
      btn.style.pointerEvents = ''
    }
  }
  let refreshing = false
  async function refreshMessages() {
    if (refreshing) return
    refreshing = true
    setBtnBusy(true)
    try {
      const res = await call('tatva_connect.api.whatsapp.refresh_messages_from_wati', {
        reference_doctype: doc.doctype, reference_name: doc.name,
      })
      notify('Synced ' + (res && res.count != null ? res.count : 0) + ' messages from WATI', true)
    } catch (e) {
      notify((e && e.message) || 'WhatsApp refresh failed', false)
    } finally {
      setBtnBusy(false)
      refreshing = false
    }
  }

  // ---- Send-Template dialog (single unified box) ----
  function sendTemplate(template, bodyParam) {
    return call('tatva_connect.api.whatsapp.send_template_with_params', {
      reference_doctype: doc.doctype, reference_name: doc.name, template,
      to: doc.mobile_no, body_param: bodyParam || null,
    })
  }

  let fieldGroups = null
  async function loadFieldGroups() {
    if (fieldGroups) return fieldGroups
    fieldGroups = (await call('tatva_connect.api.whatsapp.get_field_options', {
      reference_doctype: doc.doctype, reference_name: doc.name,
    })) || []
    return fieldGroups
  }
  function fieldOptions(q) {
    const f = (q || '').toLowerCase().trim()
    const out = []
    ;(fieldGroups || []).forEach((g) => {
      g.options.forEach((o) => {
        if (!f || (o.label + ' ' + o.value).toLowerCase().includes(f)) {
          out.push({ label: o.label, sub: g.group + (o.value ? ' · ' + o.value : ''), value: o.value })
        }
      })
    })
    return out
  }

  let currentTemplates = []
  let currentTemplate = null
  let currentVars = []

  function templateOptions(q) {
    const f = (q || '').toLowerCase().trim()
    return currentTemplates
      .filter((t) => !f || (t.label || t.name).toLowerCase().includes(f))
      .slice()
      .sort((a, b) => (a.label || a.name).localeCompare(b.label || b.name))
      .map((t) => ({
        label: t.label || t.name,
        sub: (t.category || 'OTHER') + ' · ' + (t.vars ? t.vars + ' var' + (t.vars > 1 ? 's' : '') : 'no variables'),
        value: t.name,
      }))
  }

  // Render the preview + variable inputs for the picked template, in place.
  async function renderSelected(template) {
    currentTemplate = null
    currentVars = []
    const info = await call('tatva_connect.api.whatsapp.get_template_variables', { template })
    const vars = (info && info.variables) || []
    // Real WATI paramName when scraped; graceful fallback to "Variable N" when the
    // template's param is just the numeric index (or nothing was scraped).
    const label = (v) => (v.name && v.name !== String(v.index)) ? v.name : ('Variable ' + v.index)
    const nameByIdx = {}
    vars.forEach((v) => { nameByIdx[v.index] = label(v) })
    // Preview: highlight each {{N}} as its param name (or "Variable N").
    const bodyHi = esc(info.body || '').replace(/\{\{\s*(\d+)\s*\}\}/g,
      (_m, n) => '<span class="wati-chip">' + esc(nameByIdx[n] || ('Variable ' + n)) + '</span>')
    let html = '<div class="wati-preview">' + bodyHi + '</div>'
    if (vars.length) {
      await loadFieldGroups()
      vars.forEach((v) => {
        const hint = v.hint ? ' <span class="h">(e.g. ' + esc(v.hint) + ')</span>' : ''
        html +=
          '<div class="wati-label">' + esc(label(v)) + hint + '</div>' +
          '<div class="wati-dd">' +
          '<div class="wati-ddhead"><input class="wati-field" data-txt="' + v.index + '" autocomplete="off" ' +
          'placeholder="Type a value, or pick a field…" /><span class="wati-caret">' + iconCaret + '</span></div>' +
          '<div class="wati-menu" style="display:none"></div>' +
          '</div>'
      })
    }
    whenReady('wati-selected', (el) => {
      el.innerHTML = html
      vars.forEach((v) => {
        const input = el.querySelector('[data-txt="' + v.index + '"]')
        const menu = input && input.closest('.wati-dd').querySelector('.wati-menu')
        if (input && menu) combo(input, menu, fieldOptions, (o) => { input.value = o.value })
      })
      currentTemplate = template
      currentVars = vars
    })
  }

  function resetSelection() {
    currentTemplate = null
    currentVars = []
    const input = document.getElementById('wati-tpl-input')
    if (input) input.value = ''
    const sl = document.getElementById('wati-selected')
    if (sl) sl.innerHTML = ''
  }

  async function refreshTemplates(accountName) {
    const btn = document.getElementById('wati-refresh-tpl')
    if (btn) { btn.innerHTML = iconSpin + ' Refreshing…'; btn.style.opacity = '0.7'; btn.style.pointerEvents = 'none' }
    try {
      await call('tatva_connect.whatsapp.templates_sync.sync_from_wati', { account_name: accountName })
      const ctx = await call('tatva_connect.api.whatsapp.get_send_context', {
        reference_doctype: doc.doctype, reference_name: doc.name,
      })
      currentTemplates = (ctx && ctx.templates) || []
      resetSelection()
      const hint = document.getElementById('wati-empty-hint')
      if (hint) hint.style.display = currentTemplates.length ? 'none' : ''
      notify('Synced ' + currentTemplates.length + ' template' + (currentTemplates.length === 1 ? '' : 's') + ' from WATI', true)
    } catch (e) {
      notify((e && e.message) || 'Template sync failed', false)
    } finally {
      if (btn) { btn.innerHTML = iconRefresh + ' Refresh templates'; btn.style.opacity = ''; btn.style.pointerEvents = '' }
    }
  }

  async function openTemplateDialog() {
    const ctx = await call('tatva_connect.api.whatsapp.get_send_context', {
      reference_doctype: doc.doctype, reference_name: doc.name,
    })
    const acct = ctx && ctx.account
    const to = esc((ctx && ctx.mobile_no) || '—')

    if (!acct) {
      $dialog({
        title: DIALOG_TITLE,
        html:
          '<div class="wati-meta">To: <b>' + to + '</b></div>' +
          '<div class="wati-empty">This lead has no WATI account route (Product Line / Group / Program), ' +
          'so no template can be sent. Set its routing to enable WhatsApp.</div>',
      })
      return
    }

    currentTemplates = (ctx && ctx.templates) || []
    currentTemplate = null
    currentVars = []

    $dialog({
      title: DIALOG_TITLE,
      html:
        '<div class="wati-row">' +
        '<div>' +
        '<div class="wati-meta">From: <b>' + esc(acct.name) + '</b> · ' + esc(acct.number || '—') + '</div>' +
        '<div class="wati-meta">To: <b>' + to + '</b></div>' +
        '</div>' +
        '<button id="wati-refresh-tpl" class="wati-iconbtn">' + iconRefresh + ' Refresh templates</button>' +
        '</div>' +
        '<div class="wati-dd">' +
        '<div class="wati-ddhead"><input id="wati-tpl-input" class="wati-field" autocomplete="off" ' +
        'placeholder="Search or select a template…" /><span class="wati-caret">' + iconCaret + '</span></div>' +
        '<div id="wati-tpl-menu" class="wati-menu" style="display:none"></div>' +
        '</div>' +
        '<div id="wati-empty-hint" class="wati-empty"' + (currentTemplates.length ? ' style="display:none"' : '') + '>' +
        'No approved templates synced yet — click <b>Refresh templates</b> to pull them from WATI.</div>' +
        '<div id="wati-selected"></div>',
      actions: [{
        label: 'Send', variant: 'solid',
        onClick: async (close) => {
          if (!currentTemplate) { notify('Pick a template', false); return }
          const bp = {}
          let missing = false
          currentVars.forEach((v) => {
            const el = document.querySelector('[data-txt="' + v.index + '"]')
            const val = el && el.value ? el.value.trim() : ''
            if (!val) missing = true
            bp[v.index] = val
          })
          if (currentVars.length && missing) { notify('Please fill every variable', false); return }
          await sendTemplate(currentTemplate, currentVars.length ? JSON.stringify(bp) : null)
          notify('WhatsApp template sent', true)
          close()
        },
      }],
    })
    whenReady('wati-tpl-menu', () => {
      const input = document.getElementById('wati-tpl-input')
      const menu = document.getElementById('wati-tpl-menu')
      combo(input, menu, templateOptions, (o) => {
        input.value = o.label
        renderSelected(o.value)
      })
    })
    whenReady('wati-refresh-tpl', (b) => { b.onclick = () => refreshTemplates(acct.name) })
  }

  // ---- Gate the "Refresh WhatsApp" header action on the lead's WATI route ----
  function setRefreshHidden(hidden) {
    const btn = refreshButton()
    if (btn) btn.style.display = hidden ? 'none' : ''
  }
  let routeHidden = false
  function reapplyRoute() { if (routeHidden) setRefreshHidden(true) }
  if (window.__watiRefreshGateObserver) {
    try { window.__watiRefreshGateObserver.disconnect() } catch (e) {}
  }
  const refreshObs = new MutationObserver(reapplyRoute)
  window.__watiRefreshGateObserver = refreshObs
  refreshObs.observe(document.body, { childList: true, subtree: true })
  call('tatva_connect.whatsapp.routing.lead_has_route', {
    reference_doctype: doc.doctype, reference_name: doc.name,
  })
    .then((r) => { routeHidden = !(r && r.has_route); setRefreshHidden(routeHidden) })
    .catch(() => { routeHidden = true; setRefreshHidden(true) })
  ;[150, 500, 1200].forEach((t) => setTimeout(reapplyRoute, t))

  // ---- Intercept the native "Send Template" button ----
  if (window.__watiSendTemplateHandler) {
    document.removeEventListener('click', window.__watiSendTemplateHandler, true)
  }
  window.__watiSendTemplateHandler = function (e) {
    const btn = e.target && e.target.closest ? e.target.closest('button') : null
    if (btn && (btn.textContent || '').trim() === BTN_TEXT) {
      e.stopImmediatePropagation()
      e.preventDefault()
      openTemplateDialog()
    }
  }
  document.addEventListener('click', window.__watiSendTemplateHandler, true)

  return { actions: [{ label: 'Refresh WhatsApp', onClick: refreshMessages }] }
}
