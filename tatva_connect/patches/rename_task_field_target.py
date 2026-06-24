"""Rename CRM Task Type Field.first_class_target -> target before the doctype JSON syncs, preserving values (pre-model-sync); idempotent."""
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
