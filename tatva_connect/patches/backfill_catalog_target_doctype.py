"""Backfill CRM Lead API Field.target_doctype from the retired child_doctype, then DROP the orphan column (Frappe never auto-drops); guarded by has_column, idempotent."""
import frappe

CATALOG = "CRM Lead API Field"


def execute():
	if not frappe.db.has_column(CATALOG, "child_doctype"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabCRM Lead API Field`
		SET target_doctype = child_doctype
		WHERE sql_source = 'child'
		  AND COALESCE(child_doctype, '') != ''
		  AND COALESCE(target_doctype, '') = ''
		"""
	)
	frappe.db.sql_ddl("ALTER TABLE `tabCRM Lead API Field` DROP COLUMN `child_doctype`")
