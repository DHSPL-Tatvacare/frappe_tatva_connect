# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Course Chapter, LMS Assignment, LMS Live Class and LMS Question left the access ledger, so their Custom DocPerm rows and ledger hash go and LMS's shipped matrix, permlevel-1 rows included, applies again."""
import frappe
from frappe.permissions import reset_perms

HANDED_BACK = ("Course Chapter", "LMS Assignment", "LMS Live Class", "LMS Question")


def execute():
	for doctype in HANDED_BACK:
		if not frappe.db.exists("DocType", doctype):
			continue
		reset_perms(doctype)
		frappe.defaults.clear_default(key=f"ledger_hash:{doctype}")  # a later return to the ledger rebuilds instead of matching a stale hash
		frappe.clear_cache(doctype=doctype)
