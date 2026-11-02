"""Rename the four `CRM Smart Setup` doctypes to `Tatva Smart Setup` in the Tatva Setup module, then drop the empty Smart Setup module, before model sync."""
import frappe

OLD_MODULE = "Smart Setup"
NEW_MODULE = "Tatva Setup"
RENAMES = (
	("CRM Smart Setup", "Tatva Smart Setup"),
	("CRM Smart Setup Root", "Tatva Smart Setup Root"),
	("CRM Smart Setup Part", "Tatva Smart Setup Part"),
	("CRM Smart Setup Item", "Tatva Smart Setup Item"),
)


def execute():
	for old, new in RENAMES:
		if frappe.db.exists("DocType", old) and not frappe.db.exists("DocType", new):
			frappe.rename_doc("DocType", old, new, show_alert=False)
		if frappe.db.exists("DocType", new):
			frappe.db.set_value("DocType", new, "module", NEW_MODULE)
	if frappe.db.exists("Module Def", OLD_MODULE) and not frappe.db.count("DocType", {"module": OLD_MODULE}):
		frappe.delete_doc("Module Def", OLD_MODULE, force=True, ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
