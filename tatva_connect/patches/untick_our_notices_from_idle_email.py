"""The notices we write leave the email list of every person whose email is off: frappe pre-ticked them unasked, so a person who never turned email on starts with them off on every channel. A person with email on keeps their ticks. Idempotent."""
import frappe

from tatva_connect.notifications import api, dispatch


def execute():
	idle = frappe.get_all(api.USER_SETTINGS, filters={api._EMAIL_MASTER: 0}, pluck="name")
	if idle:
		frappe.db.delete("Notification Type Preference", {
			"parenttype": api.USER_SETTINGS, "parentfield": api.EMAIL_TYPES_FIELD,
			"parent": ["in", idle], "notification_type": ["in", list(dispatch.TYPES)],
		})
