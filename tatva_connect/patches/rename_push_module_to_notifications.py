"""Collapse the `Push Notifications` module into unified `Notifications`: repoint its doctypes before model sync reads the relocated JSONs, then drop the empty old Module Def; pre-model-sync, idempotent."""
import frappe
from frappe.installer import add_module_defs

OLD = "Push Notifications"
NEW = "Notifications"
DOCTYPES = (
	"CRM Push Settings",
	"CRM Push Subscription",
	"CRM Notification Preference",
	"CRM Notification Subscription",
)


def execute():
	add_module_defs("tatva_connect", ignore_if_duplicate=True)  # ensure `Notifications` exists
	for dt in DOCTYPES:
		if frappe.db.exists("DocType", dt):
			frappe.db.set_value("DocType", dt, "module", NEW)
	if frappe.db.exists("Module Def", OLD) and not frappe.db.count("DocType", {"module": OLD}):
		frappe.delete_doc("Module Def", OLD, force=True, ignore_permissions=True)
