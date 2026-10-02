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

The bell row is frappe's Notification Log (crm's `notify_user` writes it). An assignment and an inbound
WhatsApp message get theirs elsewhere, so only a missed call or a task falling due writes one here.
Presence then picks exactly one live channel, so a rep is never banner-ed twice.
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


def write_bell(bell_type, user, actor, text, source, target):
	"""One bell row through crm's `notify_user`, which writes frappe's Notification Log; doc events fire once and the sweep stamps what it told."""
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
		}
	)


def armed(event_key) -> bool:
	"""Is this notification switched on — the operator's master and its own checkbox. The ONE gate every trigger asks first."""
	event = catalog.get(event_key)
	return bool(event) and automation.is_enabled(catalog.MASTER) and bool(frappe.get_cached_doc(catalog.ORG_SETTINGS).get(event.field))


def notify(event_key, users, title, body, data=None, bell=None) -> list:
	"""Returns the reps told, by bell or push; the sweep stamps a task only when that list is non-empty."""
	if not armed(event_key):
		return []
	event = catalog.get(event_key)
	# The bell is frappe's Notification Log, so frappe's own gates (system notifications, per-type email) decide who gets it; push stays opt-in.
	belled = list(users) if event.bell_type and bell else []
	for user in belled:
		write_bell(event.bell_type, user, bell["actor"], bell["text"], bell["source"], bell["target"])
	recipients = prefs.subscribers(event_key, users)

	tokens = []
	for user in recipients:
		# One linear chain, presence picks exactly one live channel (no double-banner).
		if event.urgency == "always_push":
			tokens += presence.all_devices(user)
		elif presence.is_present(user):
			_toast(user, title, body, data)
		else:
			tokens += presence.absent_devices(user)
	_push(tokens, title, body, data)  # one worker job per notification, none when no device is due
	return list(dict.fromkeys([*belled, *recipients]))
