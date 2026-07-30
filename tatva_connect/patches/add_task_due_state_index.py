"""Composite index (status, due_date) on CRM Task — the exact pair every due-state predicate narrows on,
and the pair `dashboard/team_charts` already counted unindexed. CRM Task's existing indexes lead with
`reference_docname`, so neither can serve a status-then-date seek. Doctype JSON cannot express a
composite index. Idempotent (has_index guard). install-app baselines patches.txt without running it, so
this also runs on after_migrate via schema_setup (mirrors add_crm_task_metrics_index)."""

import frappe

_TABLE = "tabCRM Task"
_INDEXES = (("ix_status_due_date", ("status", "due_date")),)


def execute():
	if not frappe.db.table_exists("CRM Task"):
		return
	for name, columns in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		try:
			frappe.db.add_index("CRM Task", list(columns), name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"list_engine: index {name} failed")
