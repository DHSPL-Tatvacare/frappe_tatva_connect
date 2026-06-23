"""Carry child rows' `child_doctype` onto `target_doctype` before the column is dropped.

`CRM Lead API Field` drops the redundant `child_doctype` column (target_doctype is the single
source for the field's doctype on every sql_source). The smartview composer already prefers
target_doctype and falls back to child_doctype for child rows, so existing child rows that only
populated child_doctype would lose their join target the moment the column is gone.

This backfills `target_doctype = child_doctype` for child rows whose target_doctype is blank,
BEFORE the doctype JSON syncs and drops the column — so it MUST run in [pre_model_sync].
Idempotent + guarded: a no-op once child_doctype is gone or every child row has a target_doctype.
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
