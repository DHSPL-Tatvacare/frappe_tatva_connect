"""Presence: one redis hash per user, `presence:{user}` = {"device|tab": last-seen epoch}, read by dispatch to toast a present user and push an absent one; any read error fails closed to absent."""
import datetime
import time

import frappe
from frappe.utils import convert_utc_to_system_timezone, safe_decode

# Structural, not config: a heartbeat lands every ~30s, so 90s tolerates two missed beats
# before a quiet browser is treated as away. The disconnect backstop, not an operator knob.
PRESENCE_TTL_SECONDS = 90

SUBSCRIPTION = "CRM Push Subscription"
_KEY = "presence:{user}"


def _key(user):
	return _KEY.format(user=user)


def _field(device_id, tab_id):
	"""One field per TAB: closing one tab must not mark the browser away while another tab of it is still open."""
	return f"{device_id}|{tab_id or ''}"


@frappe.whitelist()
def mark_present(device_id, tab_id=None):
	"""Heartbeat: this browser's socket is up and its tab is visible. Refresh the TTL."""
	user = frappe.session.user
	if user == "Guest" or not device_id:
		return
	frappe.cache.hset(_key(user), _field(device_id, tab_id), time.time())
	frappe.cache.expire_key(_key(user), PRESENCE_TTL_SECONDS)  # a user nobody hears from drops whole


@frappe.whitelist()
def mark_away(device_id, tab_id=None):
	"""Beacon on pagehide/hidden: this browser left. Drop the device (TTL would catch it anyway)."""
	user = frappe.session.user
	if user == "Guest" or not device_id:
		return
	frappe.cache.hdel(_key(user), _field(device_id, tab_id))


def _beats(user) -> dict:
	"""`{"device|tab": last-seen epoch}` from the user's own hash; empty on a Redis error (fail-closed to absent)."""
	try:
		return frappe.cache.hgetall(_key(user))
	except Exception:
		return {}  # fail-closed to absent: FCM still fires, nothing is dropped, and a check-in records no last-seen


def present_devices(user) -> set:
	"""Device ids seen inside the TTL, from the user's own hash; empty on a Redis error (fail-closed to absent)."""
	cutoff = time.time() - PRESENCE_TTL_SECONDS
	return {safe_decode(field).split("|", 1)[0] for field, at in _beats(user).items() if at >= cutoff}


def last_seen(user):
	"""When any of the user's tabs last beat, in the site's timezone, or None when the hash holds nothing."""
	beats = _beats(user)
	if not beats:
		return None
	at = datetime.datetime.fromtimestamp(max(beats.values()), datetime.timezone.utc)
	return convert_utc_to_system_timezone(at).replace(tzinfo=None)


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
	return frappe.get_all(SUBSCRIPTION, filters={"user": user}, pluck="fcm_token", ignore_permissions=True)  # authz-ok: tier-a — presence heartbeat, engine-written
