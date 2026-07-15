"""Composite indexes the signal inbox's consume/lookup path needs, which doctype JSON cannot express
(each field only gets its own single-column `search_index`).

  * `CRM Workflow Signal (subject_name, signal_name, status)` — the `_consume_signal` / reconciler lookup:
    the first Pending row for a (subject, signal).
  * `CRM Workflow Signal (creation)` — retention.

Idempotent (has_index guard). install-app baselines patches.txt without running it, so this also runs on
after_migrate via schema_setup (mirrors add_workflow_instance_indexes)."""
import frappe

_SIGNAL = "CRM Workflow Signal"

# (doctype, index name, columns)
_INDEXES = (
	(_SIGNAL, "ix_subject_signal_status", ["subject_name", "signal_name", "status"]),
	(_SIGNAL, "ix_creation", ["creation"]),
)


def execute():
	for doctype, name, columns in _INDEXES:
		# A doctype whose JSON has not synced yet on this pass, or an index a racing DDL already built,
		# must not abort the migrate — the next after_migrate re-runs this.
		if not frappe.db.table_exists(doctype) or frappe.db.has_index(f"tab{doctype}", name):
			continue
		try:
			frappe.db.add_index(doctype, columns, name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"workflow: index {name} on {doctype} failed")
