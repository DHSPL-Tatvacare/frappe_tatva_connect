"""Composite indexes the automation queue's own queries need, which doctype JSON cannot express (each
field only gets its own single-column `search_index` — kept on both fields for that reason).

  * `CRM Automation Resume (status, resume_at)` — the sweep's `status=Pending, resume_at<=now` scan.
  * `CRM Automation Resume (rule, status)` — `versions.plan_migration` / `versions.retire`, which scan a
    single rule's Pending executions on every rule save and delete.
  * `CRM Automation Rule Version (rule, definition_hash)` UNIQUE — the content-addressing guarantee.
    `versions.ensure_version` already looks before it inserts; this makes the invariant structural.
  * `CRM Automation Rule Version (rule, is_current)` — `versions.current_name`, the hot trigger path.

Idempotent (SHOW INDEX guard). install-app baselines patches.txt without running it, so this also runs
on after_migrate via schema_setup (mirrors add_crm_task_metrics_index / add_observability_indexes).
"""
import frappe

_RESUME = "CRM Automation Resume"
_VERSION = "CRM Automation Rule Version"

# (doctype, index name, columns, unique)
_INDEXES = (
	(_RESUME, "ix_status_resume_at", ["status", "resume_at"], False),
	(_RESUME, "ix_rule_status", ["rule", "status"], False),
	(_VERSION, "ix_rule_current", ["rule", "is_current"], False),
	(_VERSION, "unique_rule_definition", ["rule", "definition_hash"], True),
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
			frappe.log_error(frappe.get_traceback(), f"automation: index {name} on {doctype} failed")
