"""Referential guard: the catalog's master names a registry row and every event's `field` exists on both settings doctypes, or `bench migrate` fails."""
import frappe

from tatva_connect.automation.registry import AUTOMATIONS
from tatva_connect.notifications.catalog import (
	CHANNELS,
	EVENTS,
	MASTER,
	ORG_SETTINGS,
	USER_PUSH_MASTER,
	USER_SETTINGS,
)


def assert_registered():
	if MASTER not in {auto.key for auto in AUTOMATIONS}:
		frappe.throw(f"Notification drift: master switch '{MASTER}' has no row in tatva_connect/automation/registry.py.")
	if not frappe.get_meta(USER_SETTINGS).has_field(USER_PUSH_MASTER):
		frappe.throw(f"Notification drift: {USER_SETTINGS} does not declare the push master '{USER_PUSH_MASTER}'.")
	for event in EVENTS:
		for doctype in (ORG_SETTINGS, USER_SETTINGS):
			if not frappe.get_meta(doctype).has_field(event.field):
				frappe.throw(f"Notification drift: event '{event.key}' names field '{event.field}', which {doctype} does not declare.")
		unknown = set(event.channels) - set(CHANNELS)
		if unknown:
			frappe.throw(f"Notification drift: event '{event.key}' has unknown channel(s) {sorted(unknown)}. Known channels: {list(CHANNELS)}.")
