"""Rename the WATI-anchored WhatsApp doctypes to a provider-neutral spine and replace the custom_is_wati flag with the custom_provider Select (pre-model-sync); idempotent."""
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


def _column_exists(table, column):
	"""True if the column physically exists; checked via information_schema (frappe.db.has_column can report stale after a raw drop)."""
	return bool(
		frappe.db.sql(
			"""SELECT 1 FROM information_schema.COLUMNS
			   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s""",
			(table, column),
		)
	)


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
	if _column_exists("tabWhatsApp Account", "custom_is_wati"):
		frappe.db.sql(
			"""UPDATE `tabWhatsApp Account`
			   SET custom_provider = IF(custom_is_wati = 1, 'WATI', 'Meta')
			   WHERE COALESCE(custom_provider, '') = ''"""
		)
		if frappe.db.exists("Custom Field", "WhatsApp Account-custom_is_wati"):
			frappe.delete_doc("Custom Field", "WhatsApp Account-custom_is_wati", force=True)
		# Deleting the Custom Field does not drop the DB column — remove the orphan explicitly.
		if _column_exists("tabWhatsApp Account", "custom_is_wati"):
			frappe.db.sql_ddl("ALTER TABLE `tabWhatsApp Account` DROP COLUMN `custom_is_wati`")
