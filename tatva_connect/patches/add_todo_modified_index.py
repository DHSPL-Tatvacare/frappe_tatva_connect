"""Index (modified) on ToDo: the analytics delta reads ToDos changed since its last run, and ToDo carries no modified index.
Idempotent (has_index guard). install-app baselines patches.txt without running it, so this also runs on after_migrate via
schema_setup (mirrors add_task_due_state_index)."""

import frappe

_TABLE = "tabToDo"
_INDEXES = (("ix_todo_modified", ("modified",)),)


def execute():
	if not frappe.db.table_exists("ToDo"):
		return
	for name, columns in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		try:
			frappe.db.add_index("ToDo", list(columns), name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"tatva_analytics: index {name} failed")
