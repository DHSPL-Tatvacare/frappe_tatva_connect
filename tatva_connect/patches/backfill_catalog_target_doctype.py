"""Carry child rows' `child_doctype` onto `target_doctype`, then DROP the redundant column.

`CRM Lead API Field` retires the redundant `child_doctype` column (target_doctype is the single
source for the field's doctype on every sql_source). The smartview composer now reads target_doctype
only, so existing child rows that only populated child_doctype would lose their join target.

Removing the field from the doctype JSON makes Frappe *ignore* it but does NOT drop the physical
column — Frappe never auto-drops columns. So this patch must do both, explicitly:
  1. backfill target_doctype = child_doctype for child rows whose target_doctype is blank, then
  2. DROP the orphan column.
Runs in [pre_model_sync] (before model-sync). Idempotent + guarded: once the column is gone the
has_column check returns early, so a re-run / fresh install is a clean no-op.
"""
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
