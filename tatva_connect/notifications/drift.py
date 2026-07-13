"""Referential guard — keeps the notification catalog in lockstep with the automation plane.

Every `NotifiableEvent.automation_key` MUST name an `Auto` row in `automation/registry.py`
(so the ONE global gate `is_enabled(automation_key)` can never point at a missing row),
and every event channel MUST be a known channel. A drift either way fails `bench migrate`.

Runs on after_migrate, beside the automation drift check.
"""
import frappe

from tatva_connect.automation.registry import AUTOMATIONS
from tatva_connect.notifications.catalog import CHANNELS, EVENTS


def assert_registered():
	automation_keys = {auto.key for auto in AUTOMATIONS}
	for event in EVENTS:
		if event.automation_key not in automation_keys:
			frappe.throw(
				f"Notification drift: event '{event.key}' points at automation_key "
				f"'{event.automation_key}', which has no CRM Tatva Automation registry row. "
				f"Add an `Auto` in tatva_connect/automation/registry.py."
			)
		unknown = set(event.channels) - set(CHANNELS)
		if unknown:
			frappe.throw(
				f"Notification drift: event '{event.key}' has unknown channel(s) {sorted(unknown)}. "
				f"Known channels: {list(CHANNELS)}."
			)
