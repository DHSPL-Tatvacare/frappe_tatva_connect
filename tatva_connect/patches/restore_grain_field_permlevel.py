# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Product Line and Group are manager-only fields (permlevel 1 on the Custom Field), but two property setters dropped them to 0 — so any rep could move a lead into another business. They were added to get the fields back on screen (361a71e), which they had disappeared from because no role held a permlevel-1 read; the grant in access/lockdown.py fixes that properly, so the overrides go. A fresh site never carries them (they are out of the fixture file); this is the heal for a site that already does."""
import frappe

STALE = (
	"CRM Lead-custom_vertical-permlevel",
	"CRM Lead-custom_group-permlevel",
)


def execute():
	for name in STALE:
		if frappe.db.exists("Property Setter", name):
			frappe.delete_doc("Property Setter", name, force=True, ignore_permissions=True)
	frappe.clear_cache()
