# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Tick `read_only` on every `source = Lead` row declared before the column existed.

Until now a lead field on an activity form was read-only because the ENGINE forced it, so no declaration
ever had to say so. The engine now reads the declaration, which would silently open every one of those
fields for editing — and an edit writes back to the patient record. The capability ships off: what was
locked stays locked, and a form that wants an answer says so.
"""
import frappe


def execute():
	if not frappe.db.has_column("CRM Task Type Field", "read_only"):
		return  # skip-until-ready: the column arrives with sync_fixtures, after post-model-sync patches
	frappe.db.sql("""
		UPDATE `tabCRM Task Type Field` SET `read_only` = 1
		WHERE `source` = 'Lead' AND IFNULL(`read_only`, 0) = 0
	""")
