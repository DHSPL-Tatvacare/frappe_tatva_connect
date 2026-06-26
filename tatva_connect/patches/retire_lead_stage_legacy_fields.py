"""Retire dead CRM Lead lifecycle fields superseded by grain-scoped custom_stage: remove BOTH the Custom Field doc and the physical column (fixture sync drops neither); idempotent, forward-only."""
import frappe

LEAD = "CRM Lead"
_STALE = (
	"custom_program_lifecycle_stage",
	"custom_lsq_lead_stage_legacy",
	# Phase 4: the old "Main Stage" parent — superseded by the derived custom_stage
	# (the leaf rep-pick is now custom_substage). No fixture defines it -> orphan; drop it.
	"custom_main_stage",
	# Post-Phase-4: custom_stage_reason had no LSQ source (not in the parity map) and no
	# picklist category to fill it — the reason is already baked into the granular sub-stage
	# value. Removed from fixtures; drop the orphan column + Custom Field doc.
	"custom_stage_reason",
)


def execute():
	for fieldname in _STALE:
		cf = "CRM Lead-" + fieldname
		if frappe.db.exists("Custom Field", cf):
			frappe.delete_doc("Custom Field", cf, ignore_permissions=True, force=True)
		# sql_ddl, not sql: a bare ALTER trips frappe's implicit-commit guard on the after_migrate path; deleting the Custom Field leaves the column.
		if frappe.db.has_column(LEAD, fieldname):
			frappe.db.sql_ddl("ALTER TABLE `tabCRM Lead` DROP COLUMN `{0}`".format(fieldname))
