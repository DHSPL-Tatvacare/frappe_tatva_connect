"""Composite indexes the workflow runtime's own queries need, which doctype JSON cannot express (each
field only gets its own single-column `search_index`).

  * `CRM Workflow Instance (status, resume_at)` — the timer sweep's due-Parked scan (Phase 2).
  * `CRM Workflow Instance (subject_name, awaiting_signal)` — signal delivery lookup (Phase 2).
  * `CRM Workflow Instance (workflow, subject_name)` — the start-guard / scope lookup.
  * `CRM Workflow Instance (status, modified)` — the retention sweep.
  * `CRM Workflow Instance (active_key)` UNIQUE — at most one LIVE instance per (workflow, subject).
    InnoDB treats multiple NULLs as distinct, so terminal rows (active_key NULL) never collide; this
    closes the double-start race at the DB itself.

Idempotent (has_index guard). install-app baselines patches.txt without running it, so this also runs on
after_migrate via schema_setup (mirrors add_resume_index / add_observability_indexes)."""
import frappe

_INSTANCE = "CRM Workflow Instance"

# (doctype, index name, columns, unique)
_INDEXES = (
	(_INSTANCE, "ix_status_resume_at", ["status", "resume_at"], False),
	(_INSTANCE, "ix_subject_awaiting_signal", ["subject_name", "awaiting_signal"], False),
	(_INSTANCE, "ix_workflow_subject", ["workflow", "subject_name"], False),
	(_INSTANCE, "ix_status_modified", ["status", "modified"], False),
	(_INSTANCE, "unique_active_key", ["active_key"], True),
)


def execute():
	for doctype, name, columns, unique in _INDEXES:
		# A doctype whose JSON has not synced yet on this pass, or an index a racing DDL already built,
		# must not abort the migrate — the next after_migrate re-runs this.
		if not frappe.db.table_exists(doctype) or frappe.db.has_index(f"tab{doctype}", name):
			continue
		try:
			if unique:
				frappe.db.add_unique(doctype, columns, constraint_name=name)
			else:
				frappe.db.add_index(doctype, columns, name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"workflow: index {name} on {doctype} failed")
