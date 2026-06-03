"""Seed the CRM Task Type records the automations reference by name."""
import frappe

SEED_TYPES = ["WhatsApp Follow-up", "Call Lead"]


def execute():
	for type_name in SEED_TYPES:
		if not frappe.db.exists("CRM Task Type", type_name):
			frappe.get_doc({"doctype": "CRM Task Type", "type_name": type_name}).insert(
				ignore_permissions=True
			)
