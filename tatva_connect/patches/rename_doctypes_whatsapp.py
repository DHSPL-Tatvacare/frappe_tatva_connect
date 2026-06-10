"""Rename the WhatsApp (WATI) doctypes to the CRM * standard. pre_model_sync so the
table is renamed BEFORE model-sync imports the new-named JSON. MUST run before
rename_to_naming_standard (which renames ROWS in CRM WATI Account Routing).
Idempotent: skips if old absent / new present.
"""
import frappe
from frappe.model.rename_doc import rename_doc

RENAMES = [
	("WATI Settings", "CRM WATI Settings"),
	("WATI Account Routing", "CRM WATI Account Routing"),
]


def execute():
	for old, new in RENAMES:
		if not frappe.db.exists("DocType", old):
			continue
		if frappe.db.exists("DocType", new):
			continue
		rename_doc("DocType", old, new, force=True, show_alert=False)
	frappe.db.commit()
