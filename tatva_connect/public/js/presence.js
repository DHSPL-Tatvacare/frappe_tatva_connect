// TATVA: the ONE presence client every app loads (CRM, Helpdesk) through tatva_connect.presence.client — a visible tab with recent input is active, no input for idle_seconds is idle, a hidden tab is away; the server hears changes, a beat while active, and its TTL catches the rest.
const MARK = '/api/method/tatva_connect.presence.mark'
const AWAY = '/api/method/tatva_connect.presence.mark_away'
const INPUTS = ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart']
const DEVICE_KEY = 'tatva_presence_device_id'

let started = false

// A stable id for this browser, the same key the CRM used before, so existing ids carry over.
function deviceId() {
  try {
    let id = localStorage.getItem(DEVICE_KEY)
    if (!id) localStorage.setItem(DEVICE_KEY, (id = `web-${crypto.randomUUID()}`))
    return id
  } catch {
    return `web-${crypto.randomUUID()}` // storage blocked: an id for this page load only, which is all this browser allows
  }
}

function post(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Frappe-CSRF-Token': window.csrf_token },
    body: JSON.stringify(body),
    keepalive: true,
  }).catch(() => {}) // a lost report is caught by the server TTL, so there is nothing to retry
}

export function start(config) {
  if (started || !config) return
  started = true
  const ids = { device_id: deviceId(), tab_id: crypto.randomUUID() }
  const idleMs = config.idle_seconds * 1000
  let state = null
  let lastInput = Date.now()

  const report = (next) => {
    state = next
    post(MARK, { ...ids, state: next })
  }

  // sendBeacon survives an unloading page but cannot set the CSRF header, so the token rides in the body (frappe reads it from form_dict).
  const away = () => {
    if (state === null) return
    state = null
    const body = new Blob([JSON.stringify({ ...ids, csrf_token: window.csrf_token })], { type: 'application/json' })
    if (!navigator.sendBeacon?.(AWAY, body)) post(AWAY, ids)
  }

  const onInput = () => {
    lastInput = Date.now()
    if (state !== 'active' && document.visibilityState === 'visible') report('active')
  }

  const tick = () => {
    if (document.visibilityState !== 'visible') return
    if (Date.now() - lastInput >= idleMs) {
      if (state !== 'idle') report('idle')
    } else {
      report('active') // the beat: keeps an active tab alive inside the server TTL
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') onInput()
    else away()
  })
  window.addEventListener('pagehide', away)
  for (const name of INPUTS) window.addEventListener(name, onInput, { passive: true })
  setInterval(tick, config.beat_seconds * 1000)
  if (document.visibilityState === 'visible') report('active')
}
