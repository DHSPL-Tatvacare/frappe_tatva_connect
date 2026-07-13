"""Rename WhatsApp Message custom_wati_id -> custom_provider_message_id (neutral name for future providers): drop old Custom Field doc, then rename the column to carry data; idempotent."""
import frappe

from tatva_connect.patches import _schema


def execute():
	if frappe.db.exists("Custom Field", "WhatsApp Message-custom_wati_id"):
		frappe.delete_doc("Custom Field", "WhatsApp Message-custom_wati_id", force=True)
	if frappe.db.has_column("WhatsApp Message", "custom_wati_id") and not frappe.db.has_column(
		"WhatsApp Message", "custom_provider_message_id"
	):
		_schema.rename_column("WhatsApp Message", "custom_wati_id", "custom_provider_message_id")
	frappe.db.commit()
