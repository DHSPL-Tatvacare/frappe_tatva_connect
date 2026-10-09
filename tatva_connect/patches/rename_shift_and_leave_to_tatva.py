"""Rename `CRM Work Shift` and `CRM User Leave` to `Tatva Work Shift` and `Tatva User Leave`, before model sync."""
import frappe

RENAMES = (("CRM Work Shift", "Tatva Work Shift"), ("CRM User Leave", "Tatva User Leave"))


def execute():
	for old, new in RENAMES:
		if frappe.db.exists("DocType", old) and not frappe.db.exists("DocType", new):
			frappe.rename_doc("DocType", old, new, show_alert=False)
			# The doctype also moves module, from Lead to Tatva Connect, with its folder.
			frappe.db.set_value("DocType", new, "module", "Tatva Connect")
