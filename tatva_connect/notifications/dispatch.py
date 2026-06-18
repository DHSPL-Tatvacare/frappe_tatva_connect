"""The ONE send path — every notification trigger (and every future caller) routes here.

Single linear chain, early-return fail-closed:

    notify(grain_key, users, title, body, data):
        grain = catalog.get(grain_key)                       # unknown -> return
        if not automation.is_enabled(grain.automation_key):  # the ONE global gate -> return
            return
        recipients = prefs.subscribers(grain_key, users)     # opt-in filter (default OFF)
        for user in recipients:
            if grain.urgency == "always_push": push ALL devices
            elif presence.is_present(user):    toast the live socket (in-app, no push)
            else:                              FCM to the rep's ABSENT devices only

The bell row + its badge are NATIVE CRM's: `crm.api.todo.notify_user` writes a CRM
Notification on every assignment (ungated) — that is the persistent history layer, and we
never duplicate it. Dispatch owns ONLY the opt-in-gated LIVE channel; presence picks exactly
ONE of realtime / FCM per user, so there is never a double-banner.
"""
import frappe

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


def notify(grain_key, users, title, body, data=None):
	grain = catalog.get(grain_key)
	if not grain:
		return
	if not automation.is_enabled(grain.automation_key):
		return
	recipients = prefs.subscribers(grain_key, users)
	if not recipients:
		return

	for user in recipients:
		# One linear chain, presence picks exactly one live channel (no double-banner).
		if grain.urgency == "always_push":
			_push(presence.all_devices(user), title, body, data)
		elif presence.is_present(user):
			_toast(user, title, body, data)
		else:
			_push(presence.absent_devices(user), title, body, data)
