"""Composite index (contact, creation) on CRM Workflow Step Log — the exact pair the contact cap counts
on: one number, over a rolling window. `search_index` on the field gives a single-column `contact` index,
which makes the count a seek to the number and then a SCAN of every step ever logged against it; the
window is what has to ride in the leaf. Doctype JSON cannot express a composite index. Idempotent
(has_index guard). install-app baselines patches.txt without running it, so this also runs on
after_migrate via schema_setup (mirrors add_task_due_state_index)."""

import frappe

_TABLE = "tabCRM Workflow Step Log"
_INDEXES = (("ix_contact_creation", ("contact", "creation")),)


def execute():
	if not frappe.db.table_exists("CRM Workflow Step Log"):
		return
	for name, columns in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		try:
			frappe.db.add_index("CRM Workflow Step Log", list(columns), name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"workflow_engine: index {name} failed")
