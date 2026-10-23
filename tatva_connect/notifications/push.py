"""Push reads the Notification Log row like bell and email do: a one-minute drain sends each new row, in order and in batches, to the devices of a rep who picked its type."""
import frappe
from frappe.utils import add_to_date, now_datetime, strip_html

from tatva_connect.notifications import api, dispatch, quiet, sender

LOG, STATE = "Notification Log", "push_state"
SENT, SKIPPED = "Sent", "Skipped"
# Structural: a push older than this is stale news, so a row the drain never reached is left in the bell alone.
_WINDOW_MINUTES = 15
# One pass sends at most this many rows, oldest first; the rest wait a minute.
_BATCH = 500


def _live() -> bool:
	"""The one gate both doors ask first: the switch, then the credentials."""
	return dispatch.armed() and sender.configured()


def stamp_at_birth(doc, method=None):
	"""Notification Log before_insert: a row born inside a job that ticked push off is never pushed."""
	if _live() and quiet.muted(quiet.PUSH):
		doc.set(STATE, SKIPPED)


def drain():
	"""Every minute: the recent rows not yet pushed, stamped before they are sent so an overlapping pass never sends twice."""
	if not _live():
		return
	rows = frappe.get_all(
		LOG,
		filters={STATE: ["is", "not set"], "creation": [">=", add_to_date(now_datetime(), minutes=-_WINDOW_MINUTES)]},
		fields=["name", "for_user", "type", "subject", "link"],
		order_by="creation asc",
		limit=_BATCH,
	)
	if not rows:
		return
	users = list({r.for_user for r in rows})
	wanted, devices = _wanted(users), sender.devices(users)
	due = [r for r in rows if r.type in wanted.get(r.for_user, ()) and devices.get(r.for_user)]
	creds = sender.credentials() if due else None
	if due and not creds:
		return  # the key mints no token (the sender logs why); the rows wait for the next pass, unstamped
	due_names = {r.name for r in due}
	for state, names in ((SENT, list(due_names)), (SKIPPED, [r.name for r in rows if r.name not in due_names])):
		if names:
			frappe.db.set_value(LOG, {"name": ["in", names]}, STATE, state, update_modified=False)
	frappe.db.commit()  # the stamps land before the first send, so a crash mid-pass loses a push rather than doubling one
	for row in due:
		sender.send_to_tokens(devices[row.for_user], title=row.type, body=strip_html(row.subject or ""), data={"route": row.link or "/crm"}, creds=creds)


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

