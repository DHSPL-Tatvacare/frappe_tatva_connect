"""Rename WhatsApp Message custom_wati_id -> custom_provider_message_id (neutral name for future providers): drop old Custom Field doc, then rename the column to carry data; idempotent."""
import frappe


def execute():
	if frappe.db.exists("Custom Field", "WhatsApp Message-custom_wati_id"):
		frappe.delete_doc("Custom Field", "WhatsApp Message-custom_wati_id", force=True)
	if _column_exists("tabWhatsApp Message", "custom_wati_id") and not _column_exists(
		"tabWhatsApp Message", "custom_provider_message_id"
	):
		frappe.db.sql_ddl(
			"ALTER TABLE `tabWhatsApp Message` CHANGE `custom_wati_id` `custom_provider_message_id` varchar(140)"
		)
	frappe.db.commit()


def _column_exists(table, column):
	return bool(
		frappe.db.sql(
			"""SELECT 1 FROM information_schema.COLUMNS
			   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s""",
			(table, column),
		)
	)
