"""Opt-in resolution — the per-user gate (the operator gate lives in the automation plane).

Opt-ins are stored as `CRM Notification Subscription` child rows on a per-user
`CRM Notification Preference`. Default OFF: ABSENCE of an `enabled` row = not subscribed.
`subscribers()` is the only reader; it batches every candidate in ONE query (no N+1).
"""
import frappe

PREFERENCE = "CRM Notification Preference"
SUBSCRIPTION = "CRM Notification Subscription"


def subscribers(grain_key: str, users) -> list:
	"""Return the subset of `users` opted into `grain_key` (any enabled channel).

	One batched query over the child table joined to its parent — no per-user read.
	"""
	users = [u for u in dict.fromkeys(users) if u and u != "Guest"]
	if not users:
		return []
	opted = frappe.get_all(
		SUBSCRIPTION,
		filters={
			"parenttype": PREFERENCE,
			"parentfield": "subscriptions",
			"parent": ["in", users],
			"grain_key": grain_key,
			"enabled": 1,
		},
		pluck="parent",
		ignore_permissions=True,
	)
	subscribed = set(opted)
	return [u for u in users if u in subscribed]
