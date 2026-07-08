"""Composite index (status, resume_at) on CRM Automation Resume — backs the sweep_resume()
`status=Pending, resume_at<=now` query. Doctype JSON can't express a composite index (each field
only gets its own single-column search_index — kept on both fields for that reason). Idempotent
(SHOW INDEX guard). install-app baselines patches.txt without running it, so this also runs on
after_migrate via schema_setup (mirrors add_crm_task_metrics_index / add_observability_indexes)."""
import frappe

_TABLE = "tabCRM Automation Resume"
_INDEXES = (("ix_status_resume_at", "`status`, `resume_at`"),)


def execute():
	if not frappe.db.table_exists("CRM Automation Resume"):
		return
	for name, cols in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		try:
			frappe.db.add_index("CRM Automation Resume", [c.strip().strip("`") for c in cols.split(",")], name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"automation: index {name} failed")
