"""The ONE send path for the notices we write: the operator's switch, then one Notification Log row per rep through crm's `notify_user`; bell, email and push all read that row."""
from crm.fcrm.doctype.crm_notification.crm_notification import notify_user

from tatva_connect import automation
from tatva_connect.notifications import quiet

SWITCH = "Notify::Tatva::live"
# The Notification Types we write (fixtures/notification_type.json); frappe and crm write every other type themselves.
MISSED_CALL, TASK_DUE_SOON, TASK_OVERDUE = TYPES = ("Missed Call", "Task Due Soon", "Task Overdue")
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


def notify(bell_type, users, actor, text, source, target, link=None) -> list:
	"""One row per rep; returns the reps told, and the sweep stamps a task only when that list is non-empty."""
	if bell_type not in TYPES or not armed() or quiet.bell_muted():
		return []
	users = list(dict.fromkeys(u for u in users if u))
	for user in users:
		write_bell(bell_type, user, actor, text, source, target, link)
	return users
