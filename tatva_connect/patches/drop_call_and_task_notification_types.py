# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The `Call` and `Task` notice types become `Missed Call`, `Task Due Soon` and `Task Overdue` (fixtures): the old types go, with
their rows and any user's email choice of them. Prod never wrote one (the switch was never armed). Idempotent. No schema_setup
twin: a fresh site never has the old types."""
import frappe

_OLD = ("Call", "Task")


def execute():
	for name in _OLD:
		if not frappe.db.exists("Notification Type", name):
			continue
		frappe.db.delete("Notification Log", {"type": name})
		frappe.db.delete("Notification Type Preference", {"notification_type": name})
		frappe.delete_doc("Notification Type", name, force=True, ignore_permissions=True)
