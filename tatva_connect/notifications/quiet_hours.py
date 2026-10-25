"""A person's own quiet hours: no push and no email inside them, in their own time zone; the bell is never affected."""
import datetime
from zoneinfo import ZoneInfo

import frappe
from frappe.desk.doctype.notification_log.notification_log import NotificationLog
from frappe.utils import get_system_timezone, get_time, now_datetime

from tatva_connect.notifications import dispatch
from tatva_connect.notifications.api import USER_SETTINGS

FROM, TO, DAYS = "quiet_from", "quiet_to", "quiet_days"
# frappe's own Monday-to-Sunday child table, reused for the days.
DAY_ROW = "Assignment Rule Day"
WEEK = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# Where a notice being written holds its recipient's window end, for the email it sends to take as `send_after`.
_SEND_AFTER = "tatva_quiet_send_after"


def quiet_until(users) -> dict:
	"""{user: when their quiet window ends, in the site's time zone} for those of `users` inside one now; three reads for all of them."""
	if not users or not dispatch.armed():
		return {}
	# Not `is set`: frappe reads a Time of 00:00 as unset, and a window may start or end at midnight.
	rows = frappe.get_all(USER_SETTINGS, filters={"name": ["in", list(users)]}, fields=["name", FROM, TO])
	rows = [r for r in rows if r.get(FROM) is not None and r.get(TO) is not None]
	if not rows:
		return {}
	names = [r.name for r in rows]
	days = {}
	for r in frappe.get_all(DAY_ROW, filters={"parenttype": USER_SETTINGS, "parentfield": DAYS, "parent": ["in", names]}, fields=["parent", "day"]):
		days.setdefault(r.parent, set()).add(r.day)
	zones = dict(frappe.get_all("User", filters={"name": ["in", names]}, fields=["name", "time_zone"], as_list=True))
	site = ZoneInfo(get_system_timezone())
	now = now_datetime().replace(tzinfo=site)  # frappe's now is the site's wall clock
	ends = {}
	for r in rows:
		zone = ZoneInfo(zones.get(r.name)) if zones.get(r.name) else site
		end = window_end(now.astimezone(zone), get_time(r.get(FROM)), get_time(r.get(TO)), days.get(r.name, ()))
		if end:
			ends[r.name] = end.astimezone(site).replace(tzinfo=None)
	return ends


def is_on(start, stop, days) -> bool:
	"""The one rule for "quiet hours are set": both times, different, and at least one day; the screen reads it from the server."""
	return start is not None and stop is not None and bool(days) and get_time(start) != get_time(stop)


def window_end(now, start, stop, days):
	"""The end of the window `now` falls in, or None: From to To on each chosen day, a window past midnight belonging to the day it starts on."""
	if not is_on(start, stop, days):
		return None
	for back in (0, 1):
		day = now.date() - datetime.timedelta(days=back)
		if WEEK[day.weekday()] not in days:
			continue
		opens = datetime.datetime.combine(day, start, now.tzinfo)
		closes = datetime.datetime.combine(day if stop > start else day + datetime.timedelta(days=1), stop, now.tzinfo)
		if opens <= now < closes:
			return closes
	return None


class QuietNotificationLog(NotificationLog):
	"""frappe's notice, whose email waits for the end of its recipient's quiet hours; registered via `override_doctype_class`."""

	def after_insert(self):
		held = getattr(frappe.local, _SEND_AFTER, None)
		setattr(frappe.local, _SEND_AFTER, quiet_until([self.for_user]).get(self.for_user))
		try:
			super().after_insert()
		finally:
			setattr(frappe.local, _SEND_AFTER, held)  # restored, so a notice written inside another's insert keeps its own


def hold_email(doc, method=None):
	"""Email Queue before_insert: an email a notice sends inside its recipient's quiet hours waits for their end; no other email is touched."""
	if (until := getattr(frappe.local, _SEND_AFTER, None)) and not doc.send_after:
		doc.send_after = until
