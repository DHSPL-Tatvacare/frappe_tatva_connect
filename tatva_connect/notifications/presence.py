"""Presence — the CRM's own Socket.IO connection IS the presence signal, mirrored into
Redis (`frappe.cache()`) so dispatch can route a notification to where the rep actually is.

A browser heartbeats `mark_present(device_id)` every ~30s while its socket is connected and
the tab is visible; `mark_away(device_id)` (a `sendBeacon` on pagehide/hidden) deletes the
key. The TTL is the real backstop for the disconnect we never hear (sleep, crash, network
drop) — never trust an explicit `mark_away` alone. Present = ANY fresh device key exists.

`device_id` is the browser's FCM token, so presence keys subtract cleanly from the user's
FCM subscription set: `absent_devices` = registered tokens that are NOT currently present.

FAIL-CLOSED to "absent": a Redis read failure must make the user look away so FCM still
fires — a notification is never silently dropped because presence hiccuped.
"""
import frappe

# Structural, not config: a heartbeat lands every ~30s, so 90s tolerates two missed beats
# before a quiet browser is treated as away. The disconnect backstop, not an operator knob.
PRESENCE_TTL_SECONDS = 90

SUBSCRIPTION = "CRM Push Subscription"
_KEY = "presence:{user}:{device_id}"


def _key(user, device_id):
	return _KEY.format(user=user, device_id=device_id)


@frappe.whitelist()
def mark_present(device_id):
	"""Heartbeat: this browser's socket is up and its tab is visible. Refresh the TTL."""
	user = frappe.session.user
	if user == "Guest" or not device_id:
		return
	frappe.cache().set_value(_key(user, device_id), "1", expires_in_sec=PRESENCE_TTL_SECONDS)


@frappe.whitelist()
def mark_away(device_id):
	"""Beacon on pagehide/hidden: this browser left. Drop the key (TTL would catch it anyway)."""
	user = frappe.session.user
	if user == "Guest" or not device_id:
		return
	frappe.cache().delete_value(_key(user, device_id))


def present_devices(user) -> set:
	"""Device ids (FCM tokens) with a fresh presence key — scanned straight from Redis, so a
	present rep who declined push (no FCM token) still counts as present. Empty on any Redis
	error (fail-closed to absent)."""
	try:
		# get_keys appends its own "*" and site-prefixes the key; we scan "presence:{user}:".
		keys = frappe.cache().get_keys(_key(user, ""))
		return {_decode(k).split(":", 2)[-1] for k in keys}
	except Exception:
		return set()


def is_present(user) -> bool:
	"""True if ANY of the user's devices is fresh. False on any Redis error (fail-closed)."""
	return bool(present_devices(user))


def all_devices(user) -> list:
	"""Every FCM token registered for the user — the `always_push` target (presence bypassed)."""
	return _user_tokens(user)


def absent_devices(user) -> list:
	"""The user's registered FCM tokens that are NOT currently present — the OS-push targets."""
	tokens = _user_tokens(user)
	if not tokens:
		return []
	present = present_devices(user)
	return [t for t in tokens if t not in present]


def _user_tokens(user) -> list:
	"""Every FCM token registered for the user — one batched read (presence keys on token)."""
	return frappe.get_all(SUBSCRIPTION, filters={"user": user}, pluck="fcm_token", ignore_permissions=True)


def _decode(key) -> str:
	# frappe.cache().get_keys returns site-prefixed bytes; normalise to the bare presence key.
	key = key.decode() if isinstance(key, bytes) else key
	return key[key.index("presence:"):] if "presence:" in key else key
