"""`Partner::AsyncBulk::jobs` becomes `Bulk::Jobs::engine` with a partners tick, and `Lead::BulkImport::desk` becomes its import tick, each carrying its old state; the seed prunes the old rows. Idempotent."""
import frappe

# new key -> the old key whose state it carries
CARRY = {
	"Bulk::Jobs::engine": "Partner::AsyncBulk::jobs",
	"Bulk::Jobs::partners": "Partner::AsyncBulk::jobs",
	"Bulk::Jobs::import": "Lead::BulkImport::desk",
}


def execute():
	for key, old in CARRY.items():
		if frappe.db.exists("CRM Tatva Automation", key) or not frappe.db.exists("CRM Tatva Automation", old):
			continue
		# The seed runs after patches and never touches `enabled`, so the row is born here with the old state.
		doc = frappe.new_doc("CRM Tatva Automation")
		doc.automation_key = key
		doc.enabled = frappe.db.get_value("CRM Tatva Automation", old, "enabled")
		doc.insert(ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
