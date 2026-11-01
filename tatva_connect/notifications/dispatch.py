"""The ONE send path for the notices we write: the operator's switch, then one Notification Log row per rep who ticked it, through crm's `notify_user`; bell, email and push all read that row."""
import frappe
from crm.fcrm.doctype.crm_notification.crm_notification import notify_user

from tatva_connect import automation
from tatva_connect.notifications import batch
from tatva_connect.notifications.types import (  # the types we write, re-exported for every trigger
	LEAD_MOVED,
	LEAD_MOVING_SOON,
	MISSED_CALL,
	TASK_DUE_SOON,
	TASK_OVERDUE,
	TYPES,
	WORK_WAITING,
)

SWITCH = "Notify::Tatva::live"
# Toast, presence routing, the direct push and the per-type operator ticks archived in .archive/notify-one-seam-2026-10-19: every channel reads the row.


def write_bell(bell_type, user, actor, text, source, target, link=None):
	"""One row through crm's `notify_user`, which writes frappe's Notification Log; frappe's own gates (system notifications, per-type email) apply."""
	notify_user(
		{
			"owner": actor,
			"assigned_to": user,
			"notification_type": bell_type,
			"message": text,
			"notification_text": text,
			"reference_doctype": source[0],
			"reference_docname": source[1],
			"redirect_to_doctype": target[0],
			"redirect_to_docname": target[1],
			"link": link,
		}
	)


def armed() -> bool:
	"""Is the operator's switch on — the ONE gate every trigger asks first."""
	return automation.is_enabled(SWITCH)


def ticked(bell_type, users) -> set:
	"""The reps who ticked this notice for the bell, or for email or push with that channel on: the row carries all three, so nobody else gets one."""
	from tatva_connect.notifications import api

	channels = {api.BELL_TYPES_FIELD: None, api.EMAIL_TYPES_FIELD: api._EMAIL_MASTER, api.PUSH_TYPES_FIELD: api.PUSH_MASTER}
	rows = frappe.get_all(
		"Notification Type Preference",
		filters={"parenttype": api.USER_SETTINGS, "parentfield": ["in", list(channels)], "parent": ["in", users], "notification_type": bell_type},
		fields=["parent", "parentfield"],
	)
	masters = {r.name: r for r in frappe.get_all(api.USER_SETTINGS, filters={"name": ["in", users]}, fields=["name", *filter(None, channels.values())])} if rows else {}
	return {r.parent for r in rows if not channels[r.parentfield] or (masters.get(r.parent) or {}).get(channels[r.parentfield])}


def notify(bell_type, users, actor, text, source, target, link=None) -> list:
	"""One row per rep who ticked it; returns the reps told."""
	if bell_type not in TYPES or not armed() or batch.silent():
		return []
	users = list(dict.fromkeys(u for u in users if u))
	wanted = ticked(bell_type, users) if users else set()
	users = [u for u in users if u in wanted]
	for user in users:
		write_bell(bell_type, user, actor, text, source, target, link)
	return users
