"""Presence, the ONE answer to "is this person in the app right now" for every app (CRM, Helpdesk): `presence:{user}` = {"device|tab": {"state", "at"}} for live tabs, `presence_seen:{user}` = when they were last heard; push reads is_active, check-in reads last_seen; a redis error reads as never seen."""
import datetime
import os
import time

import frappe
from frappe.utils import convert_utc_to_system_timezone

from tatva_connect.access.doctype.tatva_platform_settings import tatva_platform_settings as platform_settings

# Structural: an active tab beats every BEAT_SECONDS, and TTL tolerates two missed beats before the tab reads as gone.
BEAT_SECONDS = 30
PRESENCE_TTL_SECONDS = 90
# No input for this long and a tab reads idle (Raven's rule); the default when Tatva Platform Settings leaves Idle After blank.
IDLE_SECONDS = 300
# Structural: how long "last seen" outlives the person leaving, so the next day's first check-in still reads when they were last here.
SEEN_SECONDS = 86400
ACTIVE, IDLE = "active", "idle"

_KEY = "presence:{user}"
_SEEN = "presence_seen:{user}"
_CLIENT = ("public", "js", "presence.js")


def _key(user):
	return _KEY.format(user=user)


def _field(device_id, tab_id):
	"""One field per TAB: closing one tab must not mark the browser away while another tab of it is still open."""
	return f"{device_id}|{tab_id or ''}"


@frappe.whitelist()
def client():
	"""What a signed-in app loads to report presence: the one client file, versioned by its mtime, and the thresholds; None for anyone but a desk user."""
	user = frappe.session.user
	if user == "Guest" or frappe.get_cached_value("User", user, "user_type") != "System User":
		return None
	version = int(os.path.getmtime(frappe.get_app_path("tatva_connect", *_CLIENT)))
	return {
		"url": f"/assets/tatva_connect/js/presence.js?v={version}",
		"beat_seconds": BEAT_SECONDS,
		"idle_seconds": platform_settings.presence_idle_seconds(),
	}


@frappe.whitelist(methods=["POST"])
def mark(state, device_id, tab_id=None):
	"""A tab's state, sent when it changes and as a keep-alive while active; every call refreshes the TTL."""
	user = frappe.session.user
	if user == "Guest" or not device_id or state not in (ACTIVE, IDLE):
		return
	frappe.cache.hset(_key(user), _field(device_id, tab_id), {"state": state, "at": time.time()})
	frappe.cache.expire_key(_key(user), PRESENCE_TTL_SECONDS)  # a user nobody hears from drops whole
	_heard(user)


@frappe.whitelist(methods=["POST"])
def mark_away(device_id, tab_id=None):
	"""Beacon on pagehide/hidden: this tab left. Drop it (the TTL would catch it anyway)."""
	user = frappe.session.user
	if user == "Guest" or not device_id:
		return
	frappe.cache.hdel(_key(user), _field(device_id, tab_id))
	_heard(user)


def _heard(user):
	"""Remember when the user was last heard from, past the live hash's short TTL."""
	frappe.cache.set_value(_SEEN.format(user=user), time.time(), expires_in_sec=SEEN_SECONDS)


def _tabs(user) -> list:
	"""The user's live tab entries; empty on a redis error, and any value not in today's shape reads as gone."""
	try:
		entries = frappe.cache.hgetall(_key(user)).values()
	except Exception:
		return []  # a redis error reads as nobody here: the push still goes out
	return [e for e in entries if isinstance(e, dict)]


def is_active(user) -> bool:
	"""A tab of the user's said active inside the TTL: open, visible and touched within IDLE_SECONDS."""
	now = time.time()
	return any(t["state"] == ACTIVE and now - t["at"] < PRESENCE_TTL_SECONDS for t in _tabs(user))


def last_seen(user):
	"""When the user was last heard from in any app, in the site's timezone, or None when not within SEEN_SECONDS."""
	at = frappe.cache.get_value(_SEEN.format(user=user))  # frappe's get_value already reads a redis outage as None
	if not at:
		return None
	return convert_utc_to_system_timezone(datetime.datetime.fromtimestamp(at, datetime.timezone.utc)).replace(tzinfo=None)
