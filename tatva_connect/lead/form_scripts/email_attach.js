// CRM Form Script (CRM Lead, Form view) — email/comment composer "attach" enhancement.
// The attach icon opens a popover: "from device" uses the native uploader; "from CRM" lists
// this lead's files and QUEUES the picked ones server-side. Queued files are attached to the
// mail/comment by a backend hook ON SEND (tatva_connect.api.email.on_*_insert) — they never
// enter the composer's deletable list, so Discard can never delete an original. UI only here.
function setupForm({ doc, $dialog, call, createToast }) {
  const POP_ID = 'tc-attach-pop'
  const notify = (m, ok) => createToast({ message: m, type: ok ? 'success' : 'error' })
  const esc = (s) =>
    String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

  const icon = (d) =>
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + d + '</svg>'
  const iconDevice = icon('<path d="M12 16V4"/><path d="M7 9l5-5 5 5"/><path d="M5 20h14"/>')
  const iconCrm = icon('<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/><path d="M9 21V9"/>')
  const iconFile = icon('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>')
  const iconCheck = icon('<path d="M20 6L9 17l-5-5"/>')

  if (!document.getElementById('tc-attach-style')) {
    const st = document.createElement('style')
    st.id = 'tc-attach-style'
    st.textContent =
      '#' + POP_ID + '{position:fixed;z-index:1000;min-width:208px;background:var(--surface-white);' +
      'border:1px solid var(--outline-gray-2);border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.13);padding:4px}' +
      '.tc-item{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:7px;cursor:pointer;' +
      'font-size:13px;color:var(--ink-gray-8)}' +
      '.tc-item:hover{background:var(--surface-gray-2)}.tc-item svg{color:var(--ink-gray-6);flex-shrink:0}' +
      '.tc-hint{font-size:11px;color:var(--ink-gray-5);padding:0 2px 9px}' +
      '.tc-list{display:flex;flex-direction:column;gap:2px;max-height:52vh;overflow:auto}' +
      '.tc-row{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:8px;cursor:pointer}' +
      '.tc-row:hover{background:var(--surface-gray-2)}.tc-row svg{color:var(--ink-gray-6);flex-shrink:0}' +
      '.tc-row.sel{background:var(--surface-gray-3)}' +
      '.tc-name{font-size:13px;color:var(--ink-gray-8);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '.tc-sub{font-size:11px;color:var(--ink-gray-5)}' +
      '.tc-tick{margin-left:auto;display:none;color:var(--ink-gray-7)}.tc-row.sel .tc-tick{display:inline-flex}' +
      '.tc-empty{padding:18px;text-align:center;color:var(--ink-gray-5);font-size:13px}' +
      '.tc-search{width:100%;box-sizing:border-box;border:1px solid var(--outline-gray-2);border-radius:8px;' +
      'padding:7px 10px;font-size:13px;background:var(--surface-white);color:var(--ink-gray-8);outline:none;margin-bottom:8px}' +
      '.tc-search:focus{border-color:var(--outline-gray-3)}' +
      '.tc-nomatch{display:none;padding:14px;text-align:center;color:var(--ink-gray-5);font-size:13px}'
    document.head.appendChild(st)
  }

  const humanSize = (n) => {
    n = +n || 0
    const u = ['B', 'KB', 'MB', 'GB']
    let i = 0
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++ }
    return (i ? n.toFixed(1) : n) + ' ' + u[i]
  }

  // The attach button is the slot child of a FileUploader whose root holds the file input as
  // a DIRECT child; scope to an inline composer (a TextEditor), never a dialog.
  function composerUpload(btn) {
    if (!btn || btn.closest('[role="dialog"]')) return null
    const root = btn.parentElement
    const input = root && root.querySelector(':scope > input[type="file"]')
    if (!input) return null
    for (let p = root; p; p = p.parentElement) {
      if (p.querySelector && p.querySelector('.ProseMirror,[contenteditable="true"]')) return input
    }
    return null
  }

  // The composer persists its outgoing attachments in localStorage (VueUse useStorage);
  // push a file in and fire a standard storage event so the composer re-reads it.
  function storageKey() {
    const suffix = '-' + doc.doctype + '-' + doc.name
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (k && k.indexOf('attachments-') === 0 && k.slice(-suffix.length) === suffix) return k
    }
    return null
  }
  function addToComposer(file) {
    const key = storageKey()
    if (!key) { notify('Open the reply box first', false); return }
    let list
    try { list = JSON.parse(localStorage.getItem(key) || '[]') } catch (e) { list = [] }
    if (!Array.isArray(list)) list = []
    if (list.some((f) => f && f.name === file.name)) { notify('Already attached', true); return }
    list.push({ name: file.name, file_url: file.file_url, file_name: file.file_name })
    const newValue = JSON.stringify(list)
    localStorage.setItem(key, newValue)
    window.dispatchEvent(new StorageEvent('storage', { key, newValue, storageArea: localStorage }))
    notify('Attached ' + file.file_name, true)
  }

  // "From device": upload UNATTACHED into the staging folder (not the lead), so it stays out
  // of the Attachments tab + the "from CRM" picker; on send it's attached to the mail, and on
  // discard the native cleanup deletes this brand-new file (no original is ever touched).
  function deviceUpload() {
    const inp = document.createElement('input')
    inp.type = 'file'
    inp.multiple = true
    inp.style.display = 'none'
    document.body.appendChild(inp)
    inp.addEventListener('change', async () => {
      const files = [...(inp.files || [])]
      inp.remove()
      for (const file of files) {
        try {
          const fd = new FormData()
          fd.append('file', file, file.name)
          fd.append('is_private', '1')
          fd.append('folder', 'Home/Email Drafts')
          const res = await fetch('/api/method/upload_file', {
            method: 'POST',
            headers: { 'X-Frappe-CSRF-Token': window.csrf_token || '' },
            body: fd,
          })
          const out = await res.json()
          if (out && out.message) addToComposer(out.message)
          else notify('Upload failed', false)
        } catch (e) {
          notify('Upload failed', false)
        }
      }
    })
    inp.click()
  }

  function closePop() {
    const el = document.getElementById(POP_ID)
    if (el) el.remove()
    document.removeEventListener('mousedown', onDocDown, true)
  }
  function onDocDown(e) {
    const el = document.getElementById(POP_ID)
    if (el && !el.contains(e.target)) closePop()
  }
  function openPop(btn) {
    closePop()
    const pop = document.createElement('div')
    pop.id = POP_ID
    pop.innerHTML =
      '<div class="tc-item" data-act="device">' + iconDevice + '<span>Attach from device</span></div>' +
      '<div class="tc-item" data-act="crm">' + iconCrm + '<span>Attach from CRM</span></div>'
    document.body.appendChild(pop)
    const r = btn.getBoundingClientRect()
    pop.style.left = Math.round(r.left) + 'px'
    pop.style.top = Math.round(r.bottom + 6) + 'px'
    const pr = pop.getBoundingClientRect()
    if (pr.right > window.innerWidth - 8) pop.style.left = Math.round(window.innerWidth - 8 - pr.width) + 'px'
    if (pr.bottom > window.innerHeight - 8) pop.style.top = Math.round(r.top - pr.height - 6) + 'px'
    pop.addEventListener('click', (e) => {
      const it = e.target.closest ? e.target.closest('.tc-item') : null
      if (!it) return
      closePop()
      if (it.getAttribute('data-act') === 'device') deviceUpload()
      else openCrmDialog()
    })
    setTimeout(() => document.addEventListener('mousedown', onDocDown, true), 0)
  }

  async function openCrmDialog() {
    let files = []
    try {
      files = await call('tatva_connect.api.email.list_attachable_files', {
        reference_doctype: doc.doctype, reference_name: doc.name,
      }) || []
    } catch (e) {
      notify('Could not load files', false)
      return
    }
    const byName = {}
    files.forEach((f) => { byName[f.name] = f })
    // mark files already in the composer (so re-opening the dialog can't stage a 2nd copy)
    const staged = new Set()
    const key = storageKey()
    if (key) {
      try { JSON.parse(localStorage.getItem(key) || '[]').forEach((x) => x && staged.add(x.file_name)) } catch (e) {}
    }
    const row = (f) =>
      '<div class="tc-row' + (staged.has(f.file_name) ? ' sel' : '') + '" data-name="' + esc(f.name) +
      '" data-search="' + esc((f.file_name || '').toLowerCase()) + '">' + iconFile +
      '<div style="min-width:0;flex:1"><div class="tc-name">' + esc(f.file_name) + '</div>' +
      '<div class="tc-sub">' + humanSize(f.file_size) + (f.is_private ? ' · private' : '') + '</div></div>' +
      '<span class="tc-tick">' + iconCheck + '</span></div>'
    const search = files.length
      ? '<input class="tc-search" type="text" placeholder="Search files…" autocomplete="off">'
      : ''
    const rows = files.length
      ? files.map(row).join('')
      : '<div class="tc-empty">No files on this lead yet. Use “Attach from device”.</div>'
    $dialog({
      title: 'Attach from CRM',
      html: '<div class="tc-hint">Picked files attach when you send — and are left untouched if you discard.</div>' +
        search + '<div class="tc-list">' + rows + '</div>' +
        '<div class="tc-nomatch">No matching files</div>',
      actions: [{ label: 'Done', variant: 'solid', onClick: (close) => close() }],
    })
    setTimeout(() => {
      const box = document.querySelector('.tc-search')
      const nomatch = document.querySelector('.tc-nomatch')
      if (box) {
        box.focus()
        box.addEventListener('input', () => {
          const q = box.value.trim().toLowerCase()
          let shown = 0
          document.querySelectorAll('.tc-row').forEach((el) => {
            const hit = !q || (el.getAttribute('data-search') || '').indexOf(q) !== -1
            el.style.display = hit ? '' : 'none'
            if (hit) shown++
          })
          if (nomatch) nomatch.style.display = shown ? 'none' : 'block'
        })
      }
      document.querySelectorAll('.tc-row').forEach((el) => {
        el.addEventListener('click', async () => {
          if (el.classList.contains('sel')) { notify('Already added', true); return }
          const f = byName[el.getAttribute('data-name')]
          if (!f) return
          try {
            const draft = await call('tatva_connect.api.email.stage_crm_file', {
              reference_doctype: doc.doctype, reference_name: doc.name, source_file: f.name,
            })
            if (draft) { addToComposer(draft); el.classList.add('sel') }
          } catch (e) {
            notify('Could not attach', false)
          }
        })
      })
    }, 60)
  }

  if (window.__tcAttachHandler) {
    document.removeEventListener('click', window.__tcAttachHandler, true)
  }
  window.__tcAttachHandler = function (e) {
    const btn = e.target && e.target.closest ? e.target.closest('button') : null
    if (!composerUpload(btn)) return // it's the composer's attach button
    e.stopImmediatePropagation()
    e.preventDefault()
    openPop(btn)
  }
  document.addEventListener('click', window.__tcAttachHandler, true)
}
