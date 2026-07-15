"""Composite index (custom_clinic_latitude, custom_clinic_longitude) on CRM Lead — the exact pair `location.leads_within_radius` filters `between` on. Its docstring claimed an indexed bounding-box prefilter; the fixture shipped search_index 0, so every Near Me search was a full scan. A Custom Field's search_index would give two single-column indexes, not the composite the box query wants. Idempotent (has_index guard); install-app baselines patches.txt without running it, so this also runs on after_migrate via schema_setup (mirrors add_crm_task_metrics_index)."""
import frappe

_TABLE = "tabCRM Lead"
_INDEXES = (("ix_clinic_anchor", "`custom_clinic_latitude`, `custom_clinic_longitude`"),)


def execute():
	if not frappe.db.table_exists("CRM Lead"):
		return
	for name, cols in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		columns = [c.strip().strip("`") for c in cols.split(",")]
		if not all(frappe.db.has_column("CRM Lead", c) for c in columns):
			continue
		try:
			frappe.db.add_index("CRM Lead", columns, name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"near_me: index {name} failed")
