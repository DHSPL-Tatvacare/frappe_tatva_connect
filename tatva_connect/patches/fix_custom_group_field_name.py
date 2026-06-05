"""Rename the stale Custom Field record CRM Lead-custom_psp_group -> CRM Lead-custom_group.

The group field's fieldname was changed to `custom_group`, but the Custom Field document
name kept the old `custom_psp_group`. Left as-is, importing the (conventionally-named)
`custom_field.json` fixture would try to create a SECOND custom_group field -> duplicate.
This renames the record so its name matches its fieldname, before fixtures sync.

Idempotent and safe: acts only when the stale name exists and the target does not.
"""
import frappe


def execute():
	stale = "CRM Lead-custom_psp_group"
	target = "CRM Lead-custom_group"
	if frappe.db.exists("Custom Field", stale) and not frappe.db.exists("Custom Field", target):
		frappe.rename_doc("Custom Field", stale, target, force=True)
