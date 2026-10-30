"""`Lead::CRM Lead::grain` becomes the leads tick under `Access::Grain::registry`, and tickets get a tick beside it, both carrying its state; the seed prunes the old row. Idempotent."""
import frappe

OLD = "Lead::CRM Lead::grain"
NEW = ("Access::Grain::leads", "Access::Grain::tickets")


def execute():
	if not frappe.db.exists("CRM Tatva Automation", OLD):
		return
	# A tick is armed only under an armed master, the seed's own `_disarm_orphans` rule; an insert refuses anything else.
	enabled = frappe.db.get_value("CRM Tatva Automation", OLD, "enabled") and frappe.db.get_value(
		"CRM Tatva Automation", "Access::Grain::registry", "enabled"
	)
	for key in NEW:
		if frappe.db.exists("CRM Tatva Automation", key):
			continue
		# The seed runs after patches and never touches `enabled`, so the row is born here with the old state.
		doc = frappe.new_doc("CRM Tatva Automation")
		doc.automation_key = key
		doc.enabled = enabled
		doc.insert(ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
