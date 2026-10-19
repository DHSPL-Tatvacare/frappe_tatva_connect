"""Presence: one redis hash per user, `presence:{user}` = {"device|tab": last-seen epoch}, read for a check-in's last-seen; any read error reads as never seen."""
import datetime
import time

import frappe
from frappe.utils import convert_utc_to_system_timezone, safe_decode

# Structural, not config: heartbeats land every ~30s, so 90s tolerates two missed beats before a browser reads as away.
PRESENCE_TTL_SECONDS = 90

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
	"""`{"device|tab": last-seen epoch}` from the user's own hash; empty on a Redis error."""
	try:
		return frappe.cache.hgetall(_key(user))
	except Exception:
		return {}  # a Redis error reads as never seen: a check-in records no last-seen


def last_seen(user):
	"""When any of the user's tabs last beat, in the site's timezone, or None when the hash holds nothing."""
	beats = _beats(user)
	if not beats:
		return None
	at = datetime.datetime.fromtimestamp(max(beats.values()), datetime.timezone.utc)
	return convert_utc_to_system_timezone(at).replace(tzinfo=None)


# Notification routing (is_present, present/absent/all devices) archived in .archive/notify-one-seam-2026-10-19: notifications never read presence.
