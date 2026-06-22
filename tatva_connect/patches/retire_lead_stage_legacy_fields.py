"""Retire the redundant CRM Lead lifecycle fields superseded by native grain-scoped stages.

The lead lifecycle is now the native grain-scoped stage (custom_stage, resolved per the lead's
program), so the legacy lifecycle fields are dead: custom_program_lifecycle_stage (hardcoded
oncology Select), custom_lsq_lead_stage_legacy (dead LSQ import field), and custom_substage
(folded into custom_stage). They're gone from the fixture, but Frappe's fixture sync does NOT
delete a Custom Field that disappears from the JSON — AND deleting the Custom Field doc alone
leaves the physical column behind on this MariaDB. This removes BOTH the Custom Field doc and the
physical column. Idempotent + forward-only: a no-op once they're gone.

Runs in patches.txt [post_model_sync] and schema_setup after_migrate.
"""
import frappe

LEAD = "CRM Lead"
_STALE = (
	"custom_program_lifecycle_stage",
	"custom_lsq_lead_stage_legacy",
	"custom_substage",
)


def execute():
	for fieldname in _STALE:
		cf = "CRM Lead-" + fieldname
		if frappe.db.exists("Custom Field", cf):
			frappe.delete_doc("Custom Field", cf, ignore_permissions=True, force=True)
		# sql_ddl, not sql: a bare ALTER trips frappe's implicit-commit guard on the after_migrate path
		# (same fix as retire_activity_legacy_columns). Deleting the Custom Field leaves the column.
		if frappe.db.has_column(LEAD, fieldname):
			frappe.db.sql_ddl("ALTER TABLE `tabCRM Lead` DROP COLUMN `{0}`".format(fieldname))
