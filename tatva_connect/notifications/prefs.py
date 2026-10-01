"""The user's own gate on frappe's per-user Notification Settings: `enabled`, the push master and the event's checkbox; no row = not subscribed."""
import frappe

from tatva_connect.notifications import catalog


def subscribers(event_key: str, users) -> list:
	"""The subset of `users` subscribed to `event_key`, in ONE query."""
	users = [u for u in dict.fromkeys(users) if u and u != "Guest"]
	if not users:
		return []
	subscribed = set(_subscribed(event_key, [["name", "in", users]]))
	return [u for u in users if u in subscribed]


def subscriber_users(event_key: str) -> list:
	"""Every user subscribed to `event_key` — a sweep narrows its query to these."""
	return _subscribed(event_key, [])


def _subscribed(event_key, filters) -> list:
	field = catalog.get(event_key).field
	return frappe.get_all(catalog.USER_SETTINGS, filters=[["enabled", "=", 1], [catalog.USER_PUSH_MASTER, "=", 1], [field, "=", 1], *filters], pluck="name")
