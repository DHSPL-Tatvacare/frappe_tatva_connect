"""Neutralise the WhatsApp Message provider-id field: custom_wati_id -> custom_provider_message_id.

The field lives on the shared `WhatsApp Message` doctype (the WATI adapter populates it with
WATI's stable message id, the cross-path dedup key); a neutral name lets a future provider reuse
it. Drop the old Custom Field doc (this does not drop the column), then rename the column so the
data carries over to the fixture-recreated field. Idempotent; a fresh install is a clean no-op.
"""
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
