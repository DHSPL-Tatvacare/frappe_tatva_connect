"""Referential guard: the switch names a registry row, every type we write is a Notification Type, and the rows declare every field we read, or `bench migrate` fails."""
import frappe

from tatva_connect.automation.registry import AUTOMATIONS
from tatva_connect.notifications import api, dispatch, push


def assert_registered():
	if dispatch.SWITCH not in {auto.key for auto in AUTOMATIONS}:
		frappe.throw(f"Notification drift: switch '{dispatch.SWITCH}' has no row in tatva_connect/automation/registry.py.")
	for bell_type in dispatch.TYPES:
		if not frappe.db.exists("Notification Type", bell_type):
			frappe.throw(f"Notification drift: '{bell_type}' is not a Notification Type (fixtures/notification_type.json).")
	for doctype, fields in (
		(api.USER_SETTINGS, (api.PUSH_MASTER, api.EMAIL_TYPES_FIELD, api.PUSH_TYPES_FIELD, api.LEAD_FIELD)),
		(push.LOG, (push.STATE,)),
	):
		meta = frappe.get_meta(doctype)
		for field in fields:
			if not meta.has_field(field):
				frappe.throw(f"Notification drift: {doctype} does not declare '{field}'.")
