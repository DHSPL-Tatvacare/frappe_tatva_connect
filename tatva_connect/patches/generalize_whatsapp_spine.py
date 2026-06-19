"""Provider-neutral WhatsApp spine.

Renames the WATI-anchored doctypes and replaces the `custom_is_wati` flag with the
`custom_provider` Select. Runs before model sync so the renamed doctypes and the new
field are already in place when the JSON and fixtures sync. Idempotent; a fresh install
(nothing to rename) is a clean no-op.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

_DOCTYPE_RENAMES = [
	("CRM WATI Account Routing", "CRM WhatsApp Routing"),
	("CRM WATI Settings", "CRM WhatsApp Settings"),
]


def execute():
	for old, new in _DOCTYPE_RENAMES:
		if frappe.db.exists("DocType", old) and not frappe.db.exists("DocType", new):
			frappe.rename_doc("DocType", old, new, force=True)
	_migrate_provider_field()
	frappe.db.commit()


def _migrate_provider_field():
	"""custom_is_wati (Check) -> custom_provider (Select), preserving each account's value."""
	if not frappe.db.exists("Custom Field", "WhatsApp Account-custom_provider"):
		create_custom_field(
			"WhatsApp Account",
			{
				"fieldname": "custom_provider",
				"label": "Provider",
				"fieldtype": "Select",
				"options": "WATI\nMeta",
				"insert_after": "status",
				"in_list_view": 1,
				"in_standard_filter": 1,
				"search_index": 1,
			},
		)
	if frappe.db.has_column("WhatsApp Account", "custom_is_wati"):
		frappe.db.sql(
			"""UPDATE `tabWhatsApp Account`
			   SET custom_provider = IF(custom_is_wati = 1, 'WATI', 'Meta')
			   WHERE COALESCE(custom_provider, '') = ''"""
		)
		if frappe.db.exists("Custom Field", "WhatsApp Account-custom_is_wati"):
			frappe.delete_doc("Custom Field", "WhatsApp Account-custom_is_wati", force=True)
