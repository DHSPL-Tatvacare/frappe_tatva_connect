"""Collapse the activity engine onto the 9 promoted CRM Task columns.

The activity model unified to 9 columns (see docs/plans/2026-06-21-activity-engine-unified-design.md):
the per-role columns custom_visit_status / custom_next_visit_at fold into custom_outcome /
custom_followup_at, custom_demo_scheduled_at is renamed custom_scheduled_at, the old single
custom_key_date is replaced by custom_key_date_1..4, and the 4 dead archetype child tables
(CRM Task Order/Clinical/Call/Doc Detail — defined, never written) are retired.

The new/renamed columns ship as fixtures (custom_field.json). This patch runs in BOTH patches.txt
[post_model_sync] (existing DBs) and schema_setup after_migrate (fresh installs, where patches.txt
is only baselined) — so it must be order-safe: on the post_model_sync pass the new fixture columns
may not exist yet (fixtures sync after patches), so a merge whose TARGET column is missing is
SKIPPED and its old field is left intact; the after_migrate pass (post-fixtures) then carries the
values forward and drops the old field. Idempotent and forward-only: a value already on the target
is never clobbered; a no-op once the old columns/doctypes are gone.
"""
import frappe

TASK = "CRM Task"

# old column -> merged target column. The merge runs only when BOTH columns exist; values are copied
# only where the target is still empty (NULL or ''), type-safe across Data/Datetime via CAST+NULLIF.
_MERGES = (
	("custom_visit_status", "custom_outcome"),
	("custom_next_visit_at", "custom_followup_at"),
	("custom_demo_scheduled_at", "custom_scheduled_at"),
)

# Stale fields with no merge — always safe to drop (dropping the Custom Field drops the column).
_STALE_FIELDS = (
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


def _drop_field(name):
	if frappe.db.exists("Custom Field", name):
		frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)


def execute():
	# 1) Carry values into the merged/renamed columns, then drop the old field — but ONLY once the
	#    target column exists (the new fixture has synced). If it hasn't yet, leave the old field for
	#    the after_migrate pass. NULLIF(CAST(... AS CHAR),'') treats NULL and '' as empty for both
	#    Data and Datetime columns (a bare `= ''` truncates a datetime under strict mode).
	for old, new in _MERGES:
		if not (frappe.db.has_column(TASK, old) and frappe.db.has_column(TASK, new)):
			continue
		frappe.db.sql(
			"""UPDATE `tabCRM Task`
			   SET `{new}` = `{old}`
			   WHERE NULLIF(CAST(`{old}` AS CHAR), '') IS NOT NULL
			     AND NULLIF(CAST(`{new}` AS CHAR), '') IS NULL""".format(old=old, new=new)
		)
		_drop_field("CRM Task-" + old)

	# 2) Drop the stale Custom Fields (incl. the 4 dead Table links).
	for name in _STALE_FIELDS:
		_drop_field(name)

	# 3) Drop the 4 dead archetype child doctypes + their tables (folders archived to archive/doctype/).
	for dt in _DEAD_CHILD_DOCTYPES:
		if frappe.db.exists("DocType", dt):
			frappe.delete_doc("DocType", dt, ignore_permissions=True, force=True)
