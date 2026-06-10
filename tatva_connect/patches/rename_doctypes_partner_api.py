"""Rename the Partner-API doctypes to the CRM * standard. pre_model_sync so the
table is renamed BEFORE model-sync imports the new-named JSON (else sync creates an
empty new table beside the old one). Idempotent: skips if old absent / new present.
rename_doc re-points every Link + child-table reference atomically.
"""
import frappe
from frappe.model.rename_doc import rename_doc

RENAMES = [
	("Lead API Field", "CRM Lead API Field"),
	("Lead API Mapping", "CRM Lead API Mapping"),
	("Lead API Mapping Field", "CRM Lead API Mapping Field"),
	("Lead API Mapping Program", "CRM Lead API Mapping Program"),
]


def execute():
	for old, new in RENAMES:
		if not frappe.db.exists("DocType", old):
			continue  # fresh install — sync will create `new`
		if frappe.db.exists("DocType", new):
			continue  # already renamed
		rename_doc("DocType", old, new, force=True, show_alert=False)
	frappe.db.commit()
