"""Rename CRM Task Type Field.first_class_target -> target (the 9-column field map).

The schema field that says which promoted CRM Task column a form field writes to was renamed
first_class_target -> target when the activity engine collapsed to 9 columns. This runs in
[pre_model_sync] so the column is renamed BEFORE the doctype JSON syncs — sync then sees `target`
already present (no orphaned first_class_target column, existing values preserved). Idempotent:
a no-op on a fresh install (column already `target` from the JSON) and on re-run.
"""
import frappe

TABLE = "tabCRM Task Type Field"
DT = "CRM Task Type Field"


def execute():
	if not frappe.db.has_column(DT, "first_class_target"):
		return
	if frappe.db.has_column(DT, "target"):
		# Both present (an interrupted run): drop the legacy column, keep target.
		frappe.db.sql_ddl("ALTER TABLE `{0}` DROP COLUMN `first_class_target`".format(TABLE))
		return
	# sql_ddl, not sql: a bare ALTER trips frappe's implicit-commit guard outside patch context.
	frappe.db.sql_ddl(
		"ALTER TABLE `{0}` CHANGE `first_class_target` `target` varchar(140)".format(TABLE)
	)
