"""The two facts every call needs, read from Redis instead of the database on each call.

A grain's numbers and an account's seats are read on every outbound modal and every inbound webhook, and
they change only when an operator saves Routing, an Account or an Agent — the three saves this app owns,
each of which invalidates here. The hour's expiry is the self-heal: a missed invalidation corrects itself
rather than serving yesterday's numbers.
"""
import frappe

NAMESPACE = "telephony"
TTL = 3600


def read(kind, ident, build):
	"""The cached value for one key, built and stored on a miss."""
	key = f"{NAMESPACE}:{kind}:{ident}"
	value = frappe.cache.get_value(key)
	if value is None:
		value = build()
		frappe.cache.set_value(key, value, expires_in_sec=TTL)
	return value


def invalidate():
	"""Drop every telephony key. Called from the three saves that can change what they hold."""
	frappe.cache.delete_keys(f"{NAMESPACE}:")
