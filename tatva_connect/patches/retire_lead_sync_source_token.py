"""Retire a Lead Sync Source's credential fields after moving the expiry alert to the CRM Facebook App."""
import frappe

from tatva_connect.lead_sync.notification_seed import ensure_notification

_DEAD = (
	("Custom Field", "Lead Sync Source-token_expires_on"),
	("Property Setter", "Lead Sync Source-access_token-description"),
)


def execute():
	frappe.clear_cache(doctype="CRM Facebook App")
	ensure_notification()
	for doctype, name in _DEAD:
		if frappe.db.exists(doctype, name):
			# Custom Field's own on_trash drops the column through Frappe's schema layer — not raw DDL.
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
	frappe.clear_cache(doctype="Lead Sync Source")
	frappe.db.commit()
