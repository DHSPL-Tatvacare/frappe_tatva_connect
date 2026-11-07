"""Rename `Tatva Analytics Call Fact` and `Tatva Analytics Call Summary` to `Tatva Call Fact` and `Tatva Call Summary`, before model sync."""
import frappe

RENAMES = (("Tatva Analytics Call Fact", "Tatva Call Fact"), ("Tatva Analytics Call Summary", "Tatva Call Summary"))


def execute():
	for old, new in RENAMES:
		if frappe.db.exists("DocType", old) and not frappe.db.exists("DocType", new):
			frappe.rename_doc("DocType", old, new, show_alert=False)
