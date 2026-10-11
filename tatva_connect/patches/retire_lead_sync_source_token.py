"""Retire a Lead Sync Source's credential fields: the CRM Facebook App holds the one credential."""
import frappe

from tatva_connect.patches import _schema

SOURCE = "Lead Sync Source"
_DEAD = (
	("Custom Field", f"{SOURCE}-token_expires_on"),
	("Property Setter", f"{SOURCE}-access_token-description"),
)


def execute():
	frappe.clear_cache(doctype="CRM Facebook App")
	for doctype, name in _DEAD:
		if frappe.db.exists(doctype, name):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
	# Deleting a Custom Field keeps its column (CustomField.on_trash drops only its property setters), so the dead column goes here.
	_schema.refresh(f"tab{SOURCE}")
	if frappe.db.has_column(SOURCE, "token_expires_on"):
		_schema.ddl(f"ALTER TABLE `tab{SOURCE}` DROP COLUMN `token_expires_on`", f"tab{SOURCE}")
	frappe.clear_cache(doctype=SOURCE)
	frappe.db.commit()
