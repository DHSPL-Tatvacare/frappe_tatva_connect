"""Rename CRM Task Type Field.first_class_target -> target before the doctype JSON syncs, preserving values (pre-model-sync); idempotent."""
import frappe

TABLE = "tabCRM Task Type Field"
DT = "CRM Task Type Field"


def execute():
	if not frappe.db.has_column(DT, "first_class_target"):
		return
	if frappe.db.has_column(DT, "target"):
		# Both present (an interrupted run): drop the legacy column, keep target.
		# ALLOWLIST: raw DROP COLUMN DDL — no Frappe helper
		frappe.db.sql_ddl(f"ALTER TABLE `{TABLE}` DROP COLUMN `first_class_target`")
		return
	frappe.db.rename_column(DT, "first_class_target", "target")
