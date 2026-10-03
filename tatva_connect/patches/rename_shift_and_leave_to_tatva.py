"""`CRM Work Shift` and `CRM User Leave` become `Tatva Work Shift` and `Tatva User Leave`: platform tables shared by CRM and Helpdesk.

PRE model sync, as `rename_workflow_run_to_journey`: the folders moved in source, so sync would otherwise create empty
new doctypes. `frappe.rename_doc` renames the row and table and repoints every Link to it. Neither ever reached `uat`."""
import frappe

RENAMES = (("CRM Work Shift", "Tatva Work Shift"), ("CRM User Leave", "Tatva User Leave"))


def execute():
	for old, new in RENAMES:
		if frappe.db.exists("DocType", old) and not frappe.db.exists("DocType", new):
			frappe.rename_doc("DocType", old, new, show_alert=False)
			# The doctype also moves module, from Lead to Tatva Connect, with its folder.
			frappe.db.set_value("DocType", new, "module", "Tatva Connect")
