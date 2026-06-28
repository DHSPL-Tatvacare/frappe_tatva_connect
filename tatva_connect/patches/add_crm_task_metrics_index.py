"""Composite index (reference_docname, custom_task_type, status) on CRM Task — backs the
activity-metrics recompute count query; doctype JSON can't express a composite index.
Idempotent (SHOW INDEX guard). install-app baselines patches.txt without running it, so this
also runs on after_migrate via schema_setup (mirrors add_observability_indexes)."""
import frappe

_TABLE = "tabCRM Task"
_INDEXES = (("ix_refdoc_tasktype_status", "`reference_docname`, `custom_task_type`, `status`"),)


def execute():
	if not frappe.db.table_exists("CRM Task"):
		return
	for name, cols in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		try:
			frappe.db.add_index("CRM Task", [c.strip().strip("`") for c in cols.split(",")], name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"metrics: index {name} failed")
