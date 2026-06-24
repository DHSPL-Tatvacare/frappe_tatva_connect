"""Retire dead CRM Lead lifecycle fields superseded by grain-scoped custom_stage: remove BOTH the Custom Field doc and the physical column (fixture sync drops neither); idempotent, forward-only."""
import frappe

LEAD = "CRM Lead"
_STALE = (
	"custom_program_lifecycle_stage",
	"custom_lsq_lead_stage_legacy",
	"custom_substage",
)


def execute():
	for fieldname in _STALE:
		cf = "CRM Lead-" + fieldname
		if frappe.db.exists("Custom Field", cf):
			frappe.delete_doc("Custom Field", cf, ignore_permissions=True, force=True)
		# sql_ddl, not sql: a bare ALTER trips frappe's implicit-commit guard on the after_migrate path; deleting the Custom Field leaves the column.
		if frappe.db.has_column(LEAD, fieldname):
			frappe.db.sql_ddl("ALTER TABLE `tabCRM Lead` DROP COLUMN `{0}`".format(fieldname))
