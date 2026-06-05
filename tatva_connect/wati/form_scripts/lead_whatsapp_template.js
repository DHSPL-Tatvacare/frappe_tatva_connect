// CRM Form Script (CRM Lead, Form view) — shipped as a CRM Form Script fixture.
// Hooks the built-in "Send Template" button (capture-phase click interceptor)
// and opens our param-fill dialog. No crm code touched.
function setupForm({ doc, $dialog, call, createToast }) {
  const BTN_TEXT = 'Send Template'

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
  }
  // frappe-ui toast renders the `message` prop and colours by `type`
  // ('success'/'error') — NOT `title`/`iconClasses` (those render blank).
  const notify = (msg, ok) => createToast({ message: msg, type: ok ? 'success' : 'error' })

  // One-time keyframe for the in-button spinner.
  if (!document.getElementById('wati-spin-style')) {
    const st = document.createElement('style')
    st.id = 'wati-spin-style'
    st.textContent =
      '@keyframes wati-spin{to{transform:rotate(360deg)}}' +
      '.wati-spin{animation:wati-spin .7s linear infinite;vertical-align:-2px}'
    document.head.appendChild(st)
  }

  const BTN_LABEL = 'Refresh WhatsApp'
  function refreshButton() {
    // match the header action button by its label (tolerate the busy "⟳ " prefix)
    return Array.from(document.querySelectorAll('button')).find(
      (b) => (b.textContent || '').trim().replace(/^⟳\s*/, '') === BTN_LABEL,
    )
  }
  function setBtnBusy(busy) {
    const btn = refreshButton()
    if (!btn) return
    if (busy) {
      if (!btn.dataset.watiOrig) btn.dataset.watiOrig = btn.innerHTML
      // frappe-ui's own LoadingIndicator SVG, inlined (opacity inlined since the
      // Tailwind opacity-*/animate-spin classes aren't JIT-scanned for injected DOM).
      btn.innerHTML =
        '<svg class="wati-spin" width="13" height="13" viewBox="0 0 24 24" fill="none" ' +
        'xmlns="http://www.w3.org/2000/svg">' +
        '<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" opacity="0.25"></circle>' +
        '<path fill="currentColor" opacity="0.75" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 ' +
        '5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>&nbsp;' +
        BTN_LABEL
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
    if (refreshing) return // guard against double-clicks during the (latency-prone) call
    refreshing = true
    setBtnBusy(true) // spinner INSIDE the "Refresh WhatsApp" button + disabled
    try {
      const res = await call('tatva_connect.api.whatsapp.refresh_messages_from_wati', {
        reference_doctype: doc.doctype, reference_name: doc.name,
      })
      notify('Synced ' + (res && res.count != null ? res.count : 0) + ' messages from WATI', true)
      // No page reload: refresh_messages_from_wati emits the whatsapp_message realtime
      // event, so the open WhatsApp panel re-fetches the thread inline.
    } catch (e) {
      notify((e && e.message) || 'WhatsApp refresh failed', false)
    } finally {
      setBtnBusy(false)
      refreshing = false
    }
  }

  function sendTemplate(template, bodyParam) {
    return call('tatva_connect.api.whatsapp.send_template_with_params', {
      reference_doctype: doc.doctype, reference_name: doc.name, template,
      to: doc.mobile_no, body_param: bodyParam || null,
    })
  }

  async function openParamDialog(template) {
    const [info, groups] = await Promise.all([
      call('tatva_connect.api.whatsapp.get_template_variables', { template }),
      call('tatva_connect.api.whatsapp.get_field_options', {
        reference_doctype: doc.doctype, reference_name: doc.name,
      }),
    ])
    const vars = (info && info.variables) || []
    if (!vars.length) {
      await sendTemplate(template, null)
      notify('WhatsApp template sent', true)
      return
    }
    let dlOpts = ''
    ;(groups || []).forEach((g) => {
      g.options.forEach((o) => {
        dlOpts += '<option value="' + esc(o.value) + '">' + esc(g.group + ' · ' + o.label) + '</option>'
      })
    })
    const datalist = '<datalist id="wati-fields">' + dlOpts + '</datalist>'
    // Body preview with {{N}} highlighted.
    const bodyHi = esc(info.body || '').replace(/\{\{\s*(\d+)\s*\}\}/g,
      '<span style="background:#dbeafe;color:#1d4ed8;border-radius:4px;padding:0 4px;font-weight:600">{{$1}}</span>')
    const meta =
      '<div style="font-size:12px;color:#6b7280;margin-bottom:8px">' +
      vars.length + ' variable' + (vars.length > 1 ? 's' : '') +
      ' · sending to <b style="color:#374151">' + esc(doc.mobile_no || '—') + '</b></div>'
    const body =
      '<div style="white-space:pre-wrap;background:#f9fafb;border:1px solid #eee;border-radius:8px;padding:10px;font-size:13px;color:#374151;margin-bottom:14px;max-height:150px;overflow:auto">' +
      bodyHi + '</div>'
    const rows = vars.map((v) => {
      const ex = v.hint ? ' <span style="color:#9ca3af;font-weight:400">(example: ' + esc(v.hint) + ')</span>' : ''
      return (
        '<div style="margin-bottom:14px">' +
        '<div style="font-size:13px;font-weight:600;color:#111;margin-bottom:4px">Variable {{' + v.index + '}}' + ex + '</div>' +
        '<input list="wati-fields" data-txt="' + v.index + '" autocomplete="off" ' +
        'placeholder="Type a value, or click to pick a field…" ' +
        'style="width:100%;border:1px solid #d1d5db;border-radius:6px;padding:7px 9px;font-size:14px" /></div>'
      )
    }).join('')
    $dialog({
      title: 'Send WhatsApp Template',
      html: datalist + meta + body + rows,
      actions: [{
        label: 'Send', variant: 'solid',
        onClick: async (close) => {
          const bp = {}
          let missing = false
          vars.forEach((v) => {
            const el = document.querySelector('[data-txt="' + v.index + '"]')
            const val = el && el.value ? el.value.trim() : ''
            if (!val) missing = true
            bp[v.index] = val
          })
          if (missing) { notify('Please fill every variable', false); return }
          await sendTemplate(template, JSON.stringify(bp))
          notify('WhatsApp template sent', true)
          close()
        },
      }],
    })
  }

  async function openTemplatePicker() {
    // Templates are scoped to the account this lead routes to (per WATI tenant).
    const templates = await call('tatva_connect.api.whatsapp.list_templates', {
      reference_doctype: doc.doctype, reference_name: doc.name,
    })
    if (!templates || !templates.length) {
      $dialog({
        title: 'Send WhatsApp Template',
        html:
          '<div style="font-size:13px;color:#374151;line-height:1.5">No WhatsApp templates are available for this lead.<br>' +
          '<span style="color:#6b7280">This lead has no WATI account route (Product Line / Group / Program), ' +
          'or its account has no approved templates synced yet.</span></div>',
      })
      return
    }
    const byCat = {}
    templates.forEach((t) => {
      ;(byCat[t.category] = byCat[t.category] || []).push(t)
    })
    let opts = '<option value="">Select a template…</option>'
    Object.keys(byCat).sort().forEach((cat) => {
      opts += '<optgroup label="' + esc(cat) + '">'
      byCat[cat].forEach((t) => {
        const tag = t.vars ? '  ·  ' + t.vars + ' var' + (t.vars > 1 ? 's' : '') : '  ·  no variables'
        opts += '<option value="' + esc(t.name) + '">' + esc(t.label || t.name) + tag + '</option>'
      })
      opts += '</optgroup>'
    })
    $dialog({
      title: 'Send WhatsApp Template',
      html:
        '<div style="font-size:12px;color:#6b7280;margin-bottom:8px">' +
        templates.length + ' approved template' + (templates.length > 1 ? 's' : '') +
        ' · sending to <b style="color:#374151">' + esc(doc.mobile_no || '—') + '</b></div>' +
        '<select id="wati-tpl-select" size="1" style="width:100%;border:1px solid #d1d5db;border-radius:6px;padding:7px 9px;font-size:14px">' +
        opts + '</select>',
      actions: [{
        label: 'Next', variant: 'solid',
        onClick: (close) => {
          const sel = document.getElementById('wati-tpl-select')
          const template = sel && sel.value
          if (!template) { notify('Pick a template', false); return }
          close()
          openParamDialog(template)
        },
      }],
    })
  }

  // Gate the "Refresh WhatsApp" header action on the SAME routing signal the
  // WhatsApp tab uses (tatva_connect.wati.routing.lead_has_route). The header
  // action is returned synchronously below (crm renders it once), so we hide the
  // rendered button when the lead has no WATI route. Fail-safe = hide (consistent
  // with "no route = WhatsApp blocked"); a route-check error logs server-side.
  function setRefreshHidden(hidden) {
    const btn = refreshButton()
    if (!btn) return
    btn.style.display = hidden ? 'none' : ''
  }
  let routeHidden = false
  function reapplyRoute() {
    if (routeHidden) setRefreshHidden(true)
  }
  if (window.__watiRefreshGateObserver) {
    try {
      window.__watiRefreshGateObserver.disconnect()
    } catch (e) {}
  }
  const refreshObs = new MutationObserver(reapplyRoute)
  window.__watiRefreshGateObserver = refreshObs
  refreshObs.observe(document.body, { childList: true, subtree: true })

  call('tatva_connect.wati.routing.lead_has_route', {
    reference_doctype: doc.doctype,
    reference_name: doc.name,
  })
    .then((r) => {
      routeHidden = !(r && r.has_route)
      setRefreshHidden(routeHidden)
    })
    .catch(() => {
      routeHidden = true // fail-safe: hide when we can't confirm a route
      setRefreshHidden(true)
    })
  // re-assert shortly after load (the header action can render after setup runs)
  ;[150, 500, 1200].forEach((t) => setTimeout(reapplyRoute, t))

  if (window.__watiSendTemplateHandler) {
    document.removeEventListener('click', window.__watiSendTemplateHandler, true)
  }
  window.__watiSendTemplateHandler = function (e) {
    const btn = e.target && e.target.closest ? e.target.closest('button') : null
    if (btn && (btn.textContent || '').trim() === BTN_TEXT) {
      e.stopImmediatePropagation()
      e.preventDefault()
      openTemplatePicker()
    }
  }
  document.addEventListener('click', window.__watiSendTemplateHandler, true)

  // Header action — renders in document._actions (leftmost, before Assign / Convert to Deal).
  return {
    actions: [
      {
        label: 'Refresh WhatsApp',
        onClick: refreshMessages,
      },
    ],
  }
}
