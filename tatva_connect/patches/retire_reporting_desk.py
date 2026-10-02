# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Retire the Reporting desk: its Desktop Icon, Workspace Sidebar and Workspace go together; the cards and charts it placed stay."""
import frappe

_RETIRED = (("Desktop Icon", "Reporting"), ("Workspace Sidebar", "Reporting"), ("Workspace", "Reporting"))


def execute():
	for doctype, name in _RETIRED:
		if frappe.db.exists(doctype, name):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)  # authz-ok: tier-a — patch, runs at migrate
	frappe.clear_cache()
