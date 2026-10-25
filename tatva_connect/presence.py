"""Presence, the ONE answer to "is this person in the app right now" for every app (CRM, Helpdesk): `presence:{user}` = {socket id: {"state", "at"}} for tabs in use, `presence_seen:{user}` = when they last touched the app; reports arrive over each tab's socket (`realtime/handlers.js`); push reads is_active, check-in reads both; a redis error reads as nobody here."""
import datetime
import hmac
import os
import time

import frappe
from frappe import _
from frappe.utils import cint, convert_utc_to_system_timezone, now_datetime

from tatva_connect.access.doctype.tatva_platform_settings import tatva_platform_settings as platform_settings

# No input for this long and a tab reads idle (Raven's rule); the default when Tatva Platform Settings leaves Idle After blank.
IDLE_SECONDS = 300
# Structural: how long "last seen" outlives the person leaving, so the next day's first check-in still reads when they were last here.
SEEN_SECONDS = 86400
# Structural: a hash nobody reports to any more is dropped after this long; a stale tab stops counting long before (is_active).
TABS_SECONDS = 43200
# A tab in use is active or idle; hidden and gone leave the hash.
ACTIVE, IDLE, HIDDEN, GONE = "active", "idle", "hidden", "gone"

_KEY = "presence:{user}"
_SEEN = "presence_seen:{user}"
_CLIENT = ("public", "js", "presence.js")


def _key(user):
	return _KEY.format(user=user)


@frappe.whitelist()
def client():
	"""What a signed-in app loads to report presence: the one client file, versioned by its mtime, and the idle threshold; None for anyone but a desk user."""
	user = frappe.session.user
	if user == "Guest" or frappe.get_cached_value("User", user, "user_type") != "System User":
		return None
	version = int(os.path.getmtime(frappe.get_app_path("tatva_connect", *_CLIENT)))
	return {"url": f"/assets/tatva_connect/js/presence.js?v={version}", "idle_seconds": platform_settings.presence_idle_seconds()}


@frappe.whitelist(methods=["GET"])
def report(state, tab, idle_for=None):
	"""The socket server's report of one tab's change (`realtime/handlers.js`); a GET because that server cannot send a CSRF token, so it must carry the server's secret."""
	if not _from_socket_server():
		frappe.throw(_("Only the realtime server reports presence."), frappe.PermissionError)
	if record_tab(frappe.session.user, state, tab, idle_for):
		frappe.local.flags.commit = True  # frappe rolls a GET back; the pool wake it booked must land


def record_tab(user, state, tab, idle_for=None):
	"""Store one tab's state and the user's last touch; True when this report ended an auto-away and woke the user's pools."""
	if user == "Guest" or not tab or state not in (ACTIVE, IDLE, HIDDEN, GONE):
		return False
	from tatva_connect.lead import checkin, routing  # lazy: checkin imports presence

	was_in = checkin.in_app(user)
	if state in (ACTIVE, IDLE):
		frappe.cache.hset(_key(user), tab, {"state": state, "at": time.time()})
		frappe.cache.expire_key(_key(user), TABS_SECONDS)
	else:
		frappe.cache.hdel(_key(user), tab)
	if idle_for is not None:
		_heard(user, time.time() - min(max(cint(idle_for), 0), SEEN_SECONDS))
	if was_in or not checkin.in_app(user):
		return False
	routing.wake_pools_for(user)  # leads that waited while they were away get one pass now
	checkin.tell_card(user)
	return True


def _from_socket_server():
	"""The secret only frappe's socket server holds, compared as frappe's own `realtime.get_user_info` compares it; frappe has no helper for this."""
	from frappe.realtime import get_socketio_secret

	sent = frappe.get_request_header("X-Frappe-Socket-Secret") or ""
	return hmac.compare_digest(get_socketio_secret().encode(), sent.encode())


def _heard(user, touched_at):
	"""Keep the latest touch across the user's tabs: an older one, reported later by another tab, never moves it back."""
	key = _SEEN.format(user=user)
	if touched_at > (frappe.cache.get_value(key) or 0):
		frappe.cache.set_value(key, touched_at, expires_in_sec=SEEN_SECONDS)


def _tabs(user) -> list:
	"""The user's tabs in use; empty on a redis error, and any value not in today's shape reads as gone."""
	try:
		entries = frappe.cache.hgetall(_key(user)).values()
	except Exception:
		return []  # a redis error reads as nobody here: the push still goes out
	return [e for e in entries if isinstance(e, dict)]


def is_active(user) -> bool:
	"""A tab of the user's said active within Idle After; a tab whose close was never reported (a dead socket server, a logout) ages out."""
	fresh_since = time.time() - platform_settings.presence_idle_seconds()
	return any(t["state"] == ACTIVE and t["at"] > fresh_since for t in _tabs(user))


def last_seen(user):
	"""When the user last touched any app, in the site's timezone: now while a tab of theirs is active (it reports only changes), else their last touch, or None when not within SEEN_SECONDS."""
	if is_active(user):
		return now_datetime()
	at = frappe.cache.get_value(_SEEN.format(user=user))  # frappe's get_value already reads a redis outage as None
	if not at:
		return None
	return convert_utc_to_system_timezone(datetime.datetime.fromtimestamp(at, datetime.timezone.utc)).replace(tzinfo=None)
