"""The ONE send path — every notification trigger (and every future caller) routes here.

Single linear chain, early-return fail-closed:

    notify(event_key, users, title, body, data):
        event = catalog.get(event_key)                       # unknown -> return
        if not armed(event_key):                              # master + the event's checkbox -> return
            return
        recipients = prefs.subscribers(event_key, users)     # opt-in filter (default OFF)
        for user in recipients:
            if event.urgency == "always_push": collect ALL devices
            elif presence.is_present(user):    toast the live socket (in-app, no push)
            else:                              collect the rep's ABSENT devices only
        push(collected)                        # ONE short job per notification, none if empty

The bell row is crm's. crm writes one itself on an assignment and on an inbound WhatsApp
message (ungated) — those events carry no `bell_type` here and we never write a second row.
An event crm knows nothing about (a missed call, a task falling due) has no
bell row at all, and a push with nowhere to land is a push a rep cannot act on: for those,
and ONLY those, dispatch writes the row through crm's OWN writer (`notify_user`), so the
tray is extended and never forked. Presence then picks exactly one live channel, so a rep is
never banner-ed twice.
"""
import frappe
from crm.fcrm.doctype.crm_notification.crm_notification import notify_user

from tatva_connect import automation
from tatva_connect.notifications import catalog, prefs, presence

TOAST_EVENT = "tatva_notification"  # the in-app toast realtime event (notify.js attaches a handler)


def _toast(user, title, body, data):
	# In-app toast for a live socket only — reaches a present rep, never persists. The native
	# bell row carries history regardless of how the live one landed. after_commit: only toast
	# once the trigger's transaction is durable — a rolled-back assignment must never leave a
	# phantom toast on the rep's screen.
	frappe.publish_realtime(
		TOAST_EVENT,
		{"title": title, "body": body, "route": (data or {}).get("route") or "/crm"},
		user=user,
		after_commit=True,
	)


def _push(tokens, title, body, data):
	if not tokens:
		return
	# enqueue_after_commit: don't push for a trigger that rolls back (matches the toast). The
	# job fires only once the assignment/task is durably committed.
	frappe.enqueue(
		"tatva_connect.notifications.sender.send_to_tokens",
		queue="short",
		enqueue_after_commit=True,
		tokens=tokens,
		title=title,
		body=body,
		data=data,
	)


def _bell(event, user, actor, text, source, target):
	"""One persistent tray row, written by crm's OWN writer — only for an event crm does not already bell
	itself. `notify_user` skips a rep notifying themselves; its de-dupe does NOT work (its filter keeps the
	`doctype` key, so `exists()` swallows the column error and returns None), so re-entry is OUR job to
	prevent — the doc events fire once by construction and the sweep stamps what it told."""
	notify_user(
		{
			"owner": actor,
			"assigned_to": user,
			"notification_type": event.bell_type,
			"message": text,
			"notification_text": text,
			"reference_doctype": source[0],
			"reference_docname": source[1],
			"redirect_to_doctype": target[0],
			"redirect_to_docname": target[1],
		}
	)


def armed(event_key) -> bool:
	"""Is this notification switched on — the operator's master and its own checkbox. The ONE gate every trigger asks first."""
	event = catalog.get(event_key)
	return bool(event) and automation.is_enabled(catalog.MASTER) and bool(frappe.get_cached_doc(catalog.ORG_SETTINGS).get(event.field))


def notify(event_key, users, title, body, data=None, bell=None) -> list:
	"""Returns the reps actually told — the sweep stamps a task only when that list is non-empty, so a
	stamp records that a rep was TOLD, never merely that a row was looked at."""
	if not armed(event_key):
		return []
	event = catalog.get(event_key)
	recipients = prefs.subscribers(event_key, users)
	if not recipients:
		return []

	tokens = []
	for user in recipients:
		if event.bell_type and bell:
			_bell(event, user, bell["actor"], bell["text"], bell["source"], bell["target"])
		# One linear chain, presence picks exactly one live channel (no double-banner).
		if event.urgency == "always_push":
			tokens += presence.all_devices(user)
		elif presence.is_present(user):
			_toast(user, title, body, data)
		else:
			tokens += presence.absent_devices(user)
	_push(tokens, title, body, data)  # one worker job per notification, none when no device is due
	return recipients
