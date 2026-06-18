"""Collapse the `Push Notifications` module into the unified `Notifications` module.

Push was only ever ONE transport channel; the notifications domain — catalog / dispatch /
presence / opt-in + the FCM transport + (later) email — is one self-contained module now,
mirroring `whatsapp/` and `telephony/`. On an EXISTING site the old `Push Notifications`
Module Def and its already-migrated doctypes (CRM Push Settings / CRM Push Subscription)
must be repointed to `Notifications` BEFORE model sync reads the relocated JSONs (which now
declare `module: Notifications`), or their required `module` Link is unresolvable.

pre_model_sync, idempotent, and a no-op on a fresh install (install-app creates
`Notifications` directly from modules.txt, so there is no old module to collapse).
"""
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
