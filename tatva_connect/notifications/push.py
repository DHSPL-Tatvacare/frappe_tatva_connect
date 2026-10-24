"""Push reads the Notification Log row like bell and email do: a one-minute drain sends each new row, in order and in batches, to the devices of a rep who picked its type."""
import frappe
from frappe.utils import add_days, add_to_date, now_datetime, strip_html

from tatva_connect import presence
from tatva_connect.notifications import api, dispatch, sender

LOG, STATE = "Notification Log", "push_state"
PENDING, SENT, EXPIRED = "Pending", "Sent", "Expired"
# Why a row was not pushed, stamped on it so the log says it in words.
REP_ACTIVE, NOT_PICKED, NO_DEVICE = SKIPPED = ("Skipped: rep active", "Skipped: type not picked", "Skipped: no device")
# One pass sends at most this many rows, oldest first; the rest wait a minute.
_BATCH = 500


def _live() -> bool:
	"""The one gate both doors ask first: the switch, then the credentials."""
	return dispatch.armed() and sender.configured()


def mark_pending(doc, method=None):
	"""Notification Log before_insert: a row push will read starts Pending, so its state shows from the first moment."""
	if _live():
		doc.set(STATE, PENDING)


def drain():
	"""Every minute: the recent rows not yet pushed, stamped before they are sent so an overlapping pass never sends twice."""
	if not _live():
		return
	now = now_datetime()
	cutoff = add_to_date(now, seconds=-sender.STALE_SECONDS)
	# A row no pass reached inside its window is Expired; the day floor keeps the update on the creation index.
	frappe.db.set_value(LOG, {STATE: PENDING, "creation": ["between", [add_days(now, -1), cutoff]]}, STATE, EXPIRED, update_modified=False)
	rows = frappe.get_all(
		LOG,
		filters={STATE: PENDING, "creation": [">", cutoff]},
		fields=["name", "for_user", "type", "subject", "link", "creation"],
		order_by="creation asc",
		limit=_BATCH,
	)
	if not rows:
		return
	users = list({r.for_user for r in rows})
	wanted, devices = _wanted(users), sender.devices(users)
	reachable = {r.for_user for r in rows if r.type in wanted.get(r.for_user, ()) and devices.get(r.for_user)}
	# A rep in the CRM right now sees the bell ring live, so no device of theirs buzzes too.
	active = {user for user in reachable if presence.is_active(user)}
	states = {r.name: _state(r, wanted, devices, active) for r in rows}
	due = [r for r in rows if states[r.name] == SENT]
	creds = sender.credentials() if due else None
	if due and not creds:
		return  # the key mints no token (the sender logs why); the rows wait for the next pass, unstamped
	for state in (SENT, *SKIPPED):
		names = [name for name, s in states.items() if s == state]
		if names:
			frappe.db.set_value(LOG, {"name": ["in", names]}, STATE, state, update_modified=False)
	frappe.db.commit()  # the stamps land before the first send, so a crash mid-pass loses a push rather than doubling one
	for row in due:
		sender.send_to_tokens(
			devices[row.for_user], title=row.type, body=strip_html(row.subject or ""),
			data={"route": row.link or "/crm", "tag": row.type}, creds=creds, ttl=_time_left(row),
		)


def _state(row, wanted, devices, active) -> str:
	"""What this pass does with the row, in the words the log shows."""
	if row.type not in wanted.get(row.for_user, ()):
		return NOT_PICKED
	if not devices.get(row.for_user):
		return NO_DEVICE
	return REP_ACTIVE if row.for_user in active else SENT


def _time_left(row) -> int:
	"""Seconds the notice stays worth a push, counted from when it happened, so a slow pass never stretches it; 0 tells FCM deliver now or drop."""
	return max(0, sender.STALE_SECONDS - int((now_datetime() - row.creation).total_seconds()))


def _wanted(users) -> dict:
	"""{user: the types they push}, for users whose push master is on, in two reads."""
	on = frappe.get_all(api.USER_SETTINGS, filters={"name": ["in", users], api.PUSH_MASTER: 1}, pluck="name")
	if not on:
		return {}
	wanted = {}
	for row in frappe.get_all(
		"Notification Type Preference",
		filters={"parenttype": api.USER_SETTINGS, "parentfield": api.PUSH_TYPES_FIELD, "parent": ["in", on]},
		fields=["parent", "notification_type"],
	):
		wanted.setdefault(row.parent, set()).add(row.notification_type)
	return wanted

