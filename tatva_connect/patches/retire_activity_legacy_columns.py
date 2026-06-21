"""Collapse the activity engine onto the 9 promoted CRM Task columns.

The activity model unified to 9 columns (see docs/plans/2026-06-21-activity-engine-unified-design.md):
the per-role columns custom_visit_status / custom_next_visit_at fold into custom_outcome /
custom_followup_at, custom_demo_scheduled_at is renamed custom_scheduled_at, the old single
custom_key_date is replaced by custom_key_date_1..4, and the 4 dead archetype child tables
(CRM Task Order/Clinical/Call/Doc Detail — defined, never written) are retired.

The new/renamed columns ship as fixtures (custom_field.json) and build on the same migrate;
this patch carries forward any existing values into the merged columns, then drops the stale
Custom Fields (Frappe's fixture sync never deletes a field removed from the JSON) and the 4 dead
child doctypes/tables. Idempotent: a no-op once the old columns/doctypes are gone — safe on a
fresh install (nothing to copy/drop) and on re-run.
"""
import frappe

TASK = "CRM Task"

# old column -> merged target column. Copy only where the target is still empty, so a real
# value on the new column is never clobbered (forward-only, re-runnable).
_MERGES = (
	("custom_visit_status", "custom_outcome"),
	("custom_next_visit_at", "custom_followup_at"),
	("custom_demo_scheduled_at", "custom_scheduled_at"),
)

_STALE_FIELDS = (
	"CRM Task-custom_visit_status",
	"CRM Task-custom_next_visit_at",
	"CRM Task-custom_demo_scheduled_at",
	"CRM Task-custom_key_date",
	"CRM Task-custom_order_detail",
	"CRM Task-custom_clinical_detail",
	"CRM Task-custom_call_detail",
	"CRM Task-custom_doc_detail",
)

_DEAD_CHILD_DOCTYPES = (
	"CRM Task Order Detail",
	"CRM Task Clinical Detail",
	"CRM Task Call Detail",
	"CRM Task Doc Detail",
)


def execute():
	# 1) Carry existing values into the merged/renamed columns (only where empty).
	for old, new in _MERGES:
		if frappe.db.has_column(TASK, old) and frappe.db.has_column(TASK, new):
			frappe.db.sql(
				"""UPDATE `tabCRM Task`
				   SET `{new}` = `{old}`
				   WHERE `{old}` IS NOT NULL AND `{old}` != ''
				     AND (`{new}` IS NULL OR `{new}` = '')""".format(old=old, new=new)
			)

	# 2) Drop the stale Custom Fields (this drops their CRM Task columns, incl. the 4 Table links).
	for name in _STALE_FIELDS:
		if frappe.db.exists("Custom Field", name):
			frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)

	# 3) Drop the 4 dead archetype child doctypes + their tables (archived to archive/doctype/).
	for dt in _DEAD_CHILD_DOCTYPES:
		if frappe.db.exists("DocType", dt):
			frappe.delete_doc("DocType", dt, ignore_permissions=True, force=True)
