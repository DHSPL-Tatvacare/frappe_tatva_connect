"""Presence, the ONE answer to "is this person in the app right now" for every app (CRM, Helpdesk): `presence:{user}` = {field: {"state", "at"}} for each tab in use (keyed by socket id) and each live working activity (keyed by record), `presence_seen:{user}` = when they last touched the app or finished working; tabs report over their socket (`realtime/handlers.js`), working activities through `on_working_doc`; push reads is_active, check-in reads last_seen; a redis error reads as nobody here."""
import datetime
import hmac
import os
import time
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import cint, convert_utc_to_system_timezone, get_datetime, get_system_timezone, now_datetime

from tatva_connect.access.doctype.tatva_platform_settings import tatva_platform_settings as platform_settings
from tatva_connect.propagate import fail_safe

# No input for this long and a tab reads idle (Raven's rule); the default when Tatva Platform Settings leaves Idle After blank.
IDLE_SECONDS = 300
# Structural: how long "last seen" outlives the person leaving, so the next day's first check-in still reads when they were last here.
SEEN_SECONDS = 86400
# Structural: an entry whose end was never reported is dropped after this long, and a hash nobody reports to expires; it stops counting long before.
TABS_SECONDS = 43200
# Structural: a live working activity whose end never arrived (a missed hangup) stops counting after this long.
BUSY_SECONDS = 3600
# A tab in use is active or idle and a working activity is busy while live; hidden and gone leave the hash.
ACTIVE, IDLE, HIDDEN, GONE, BUSY = "active", "idle", "hidden", "gone", "busy"

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
	"""One tab's state, with the seconds since its last real touch; True when it ended an auto-away and woke the user's pools."""
	if state not in (ACTIVE, IDLE, HIDDEN, GONE):
		return False
	touched_at = time.time() - min(max(cint(idle_for), 0), SEEN_SECONDS) if idle_for is not None else None
	return _record(user, tab, state, touched_at)


def working():
	"""What counts as working besides touching the app, by doctype: who works it, its status field, the statuses that mean live and worked, and when it ended; a new source is one entry here plus its after_insert/on_update lines in hooks.py, locked by a test."""
	# lazy: telephony pulls in storage and channels; it owns what a live call is
	from tatva_connect.telephony import writer

	return {
		writer.CALL_LOG: {"people": ("caller", "receiver"), "status": "status", "live": (writer.LIVE_STATUS,), "done": ("Completed",), "ended_at": "end_time"},
	}


@fail_safe(applies=lambda doc: doc.doctype in working())
def on_working_doc(doc, method=None):
	"""after_insert / on_update of every working doctype: its people on a live record are busy; on a finished one, or moved off it (a transferred call), they are not, and a worked one's end is their last touch."""
	source = working()[doc.doctype]
	status, field = doc.get(source["status"]), f"{doc.doctype}:{doc.name}"
	people = {doc.get(name) for name in source["people"]} - {None, ""}
	before = doc.get_doc_before_save()
	moved_off = ({before.get(name) for name in source["people"]} - {None, ""} - people) if before else set()
	ended_at = _epoch(doc.get(source["ended_at"])) if status in source["done"] else None
	for user in moved_off:
		_record(user, field, GONE, None)
	for user in people:
		_record(user, field, BUSY if status in source["live"] else GONE, ended_at)


def _record(user, field, state, touched_at):
	"""THE presence writer every reporter shares (a tab, a working activity): store or drop the entry, move the last touch forward, and when this ends an auto-away write the Away row and wake the user's pools."""
	if not field or user in (None, "", "Guest"):
		return False
	from tatva_connect.lead import checkin, routing  # lazy: checkin imports presence

	was_in = checkin.in_app(user)
	away_from = None if was_in else last_seen(user)  # the last touch before the gap, read before this report moves it
	key, now = _key(user), time.time()
	# Entries whose end never arrived (a dead socket server, a lost hangup) go once they are TABS_SECONDS old, so a hash never grows.
	drop = [name for name, entry in _entries(user).items() if not isinstance(entry, dict) or now - entry["at"] > TABS_SECONDS]
	if state in (ACTIVE, IDLE, BUSY):
		frappe.cache.hset(key, field, {"state": state, "at": now})
		frappe.cache.expire_key(key, TABS_SECONDS)
	else:
		drop.append(field)
	if drop:
		frappe.cache.hdel(key, drop)
	if touched_at is not None:
		_heard(user, min(touched_at, now))
	if was_in or not checkin.in_app(user):
		return False
	checkin.record_away(user, away_from)
	routing.wake_pools_for(user)  # leads that waited while they were away get one pass now
	checkin.tell_card(user)
	return True


def _from_socket_server():
	"""The secret only frappe's socket server holds, compared as frappe's own `realtime.get_user_info` compares it; frappe has no helper for this."""
	from frappe.realtime import get_socketio_secret

	sent = frappe.get_request_header("X-Frappe-Socket-Secret") or ""
	return hmac.compare_digest(get_socketio_secret().encode(), sent.encode())


def _epoch(moment):
	"""A site-timezone datetime as the unix time `presence_seen` keeps; frappe's `get_timestamp` drops the time and reads the machine's zone."""
	return get_datetime(moment).replace(tzinfo=ZoneInfo(get_system_timezone())).timestamp() if moment else None


def _heard(user, touched_at):
	"""Keep the latest touch across the user's tabs and activities: an older one, reported later, never moves it back."""
	key = _SEEN.format(user=user)
	if touched_at > (frappe.cache.get_value(key) or 0):
		frappe.cache.set_value(key, touched_at, expires_in_sec=SEEN_SECONDS)


def _entries(user) -> dict:
	"""The user's entries by field; empty on a redis error."""
	try:
		return frappe.cache.hgetall(_key(user))
	except Exception:
		return {}  # a redis error reads as nobody here: the push still goes out


def _tabs(user) -> list:
	"""The user's entries; any value not in today's shape reads as gone."""
	return [e for e in _entries(user).values() if isinstance(e, dict)]


def _live(entries, state, seconds):
	return any(e["state"] == state and time.time() - e["at"] < seconds for e in entries)


def is_active(user) -> bool:
	"""A tab of the user's said active within Idle After: they are looking at the app; a tab whose close was never reported ages out."""
	return _live(_tabs(user), ACTIVE, platform_settings.presence_idle_seconds())


def last_seen(user):
	"""When the user last touched any app or finished working, in the site's timezone: now while a tab is active or a working activity is live, else their last touch, or None when not within SEEN_SECONDS."""
	entries = _tabs(user)
	if _live(entries, ACTIVE, platform_settings.presence_idle_seconds()) or _live(entries, BUSY, BUSY_SECONDS):
		return now_datetime()
	at = frappe.cache.get_value(_SEEN.format(user=user))  # frappe's get_value already reads a redis outage as None
	if not at:
		return None
	return convert_utc_to_system_timezone(datetime.datetime.fromtimestamp(at, datetime.timezone.utc)).replace(tzinfo=None)
