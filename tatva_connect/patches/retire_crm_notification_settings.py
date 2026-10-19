# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""CRM Notification Settings retires: one switch gates the notices we write, and each rep picks types per channel on their own
Notification Settings row. Its Single rows and DocType go, the five per-user push ticks go with their columns, and the
Communications desk drops its link. Prod never armed a tick (every one 0). Idempotent. No schema_setup twin: a fresh site
never has any of them."""
import frappe

from tatva_connect.patches import _desk, _schema

_OLD = "CRM Notification Settings"
_USER = "Notification Settings"
_TICKS = ("push_lead_assigned", "push_whatsapp_received", "push_call_missed", "push_due_soon", "push_overdue")
_BREAKS = ("push_types_section", "push_column_1", "push_column_2")


def execute():
	frappe.db.delete("Singles", {"doctype": _OLD})
	_schema.drop_doctype(_OLD)
	for fieldname in (*_TICKS, *_BREAKS):
		if frappe.db.exists("Custom Field", f"{_USER}-{fieldname}"):
			frappe.delete_doc("Custom Field", f"{_USER}-{fieldname}", force=True, ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
	# Deleting a Custom Field keeps its column (CustomField.on_trash drops only its property setters), so the dead columns go here.
	_schema.refresh(f"tab{_USER}")
	for fieldname in _TICKS:
		if frappe.db.has_column(_USER, fieldname):
			_schema.ddl(f"ALTER TABLE `tab{_USER}` DROP COLUMN `{fieldname}`", f"tab{_USER}")
	frappe.clear_cache(doctype=_USER)
	_desk.reimport_all([
		("communications", "workspace", "communications", "communications.json"),
		("communications", "sidebar", "communications", "communications.json"),
	])
