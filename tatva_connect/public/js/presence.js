// TATVA: the ONE presence client every app loads (CRM, Helpdesk) through tatva_connect.presence.client — a visible, touched tab is active, no touch for idle_seconds is idle, a hidden tab says nothing; it reports over the app's own socket when that changes, and while touched at most every half idle_seconds so the server can age out a tab it never hears close.
const EVENT = 'tatva_presence'
const INPUTS = ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart']
// How often the tab checks itself for idle; it sends nothing unless the state changes.
const CHECK_MS = 15000

let started = false

export function start(config, socket) {
  if (started || !config || !socket) return
  started = true
  const idleMs = config.idle_seconds * 1000
  let state = null // what this tab last told the server: 'active', 'idle', or null (hidden or never touched)
  let lastInput = Date.now()
  let touched = false // a page that loads is not a touch
  let lastSent = 0

  // Seconds since the last real touch, so the server keeps that moment and not when the report arrived.
  const idleFor = () => (touched ? Math.round((Date.now() - lastInput) / 1000) : null)
  const send = (next) => {
    lastSent = Date.now()
    socket.emit(EVENT, { state: next, idle_for: idleFor() })
  }

  const report = (next) => {
    state = next
    send(next)
  }

  const hide = () => {
    if (state === null) return
    state = null
    send('hidden')
  }

  const onInput = () => {
    lastInput = Date.now()
    touched = true
    if (document.visibilityState !== 'visible') return
    if (state !== 'active' || Date.now() - lastSent >= idleMs / 2) report('active')
  }

  const check = () => {
    if (state === 'active' && Date.now() - lastInput >= idleMs) report('idle')
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') onInput()
    else hide()
  })
  window.addEventListener('pagehide', hide)
  for (const name of INPUTS) window.addEventListener(name, onInput, { passive: true })
  setInterval(check, CHECK_MS)
  // A reconnect is a new socket the server knows nothing about: a tab in use says so again, a hidden tab stays silent.
  socket.on('connect', () => state && send(state))
}
