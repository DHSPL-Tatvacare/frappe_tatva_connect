"""Retire a Lead Sync Source's credential fields: the CRM Facebook App holds the one credential."""
import frappe

_DEAD = (
	("Custom Field", "Lead Sync Source-token_expires_on"),
	("Property Setter", "Lead Sync Source-access_token-description"),
)


def execute():
	frappe.clear_cache(doctype="CRM Facebook App")
	for doctype, name in _DEAD:
		if frappe.db.exists(doctype, name):
			# Custom Field's own on_trash drops the column through Frappe's schema layer — not raw DDL.
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
	frappe.clear_cache(doctype="Lead Sync Source")
	frappe.db.commit()
