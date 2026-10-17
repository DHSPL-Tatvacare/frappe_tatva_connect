"""The Automations sidebar ships as Workflows: drop the old row once the new one is in, or the desk keeps a second shell for the module."""
import frappe


def execute():
	if frappe.db.exists("Sidebar", "Workflows") and frappe.db.exists("Sidebar", "Automations"):
		frappe.delete_doc("Sidebar", "Automations", force=True, ignore_permissions=True)  # authz-ok: tier-c — migrate removes a shipped record it replaced
