# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Drop the four copy columns from CRM Lead API Field: section routing lives once on CRM Lead Section.

`section_key`, `target_doctype`, `child_table_field` and `child_pick` were a per-row restatement of a
section's facts — the same rot rekey_lead_catalog_sections fixed for the row key. Every production reader
now derives routing from the `section` Link (lead/detail, intake, smartview, partner). rekey populated the
Link and MUST have run first; this drops the mirrors. Frappe never auto-drops a removed JSON field, so the
column is dropped here. End state: none of the four columns exist. Idempotent; assumes nothing prior.
"""
import frappe

from tatva_connect.patches import _schema

DT = "CRM Lead API Field"
TABLE = "tabCRM Lead API Field"
COPY_COLUMNS = ("section_key", "target_doctype", "child_table_field", "child_pick")


def execute():
	for col in COPY_COLUMNS:
		_schema.refresh(TABLE)
		if not frappe.db.has_column(DT, col):
			continue
		_schema.ddl(f"ALTER TABLE `{TABLE}` DROP COLUMN `{col}`", TABLE)
	frappe.db.commit()
