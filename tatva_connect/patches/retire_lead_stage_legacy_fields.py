"""Retire the redundant CRM Lead lifecycle fields superseded by native grain-scoped stages.

The lead lifecycle is now the native grain-scoped stage (custom_stage, resolved per the lead's
program), so the legacy lifecycle fields are dead: custom_program_lifecycle_stage (hardcoded
oncology Select) and custom_lsq_lead_stage_legacy (dead LSQ import field). They're gone from the
fixture, but Frappe's fixture sync does NOT delete a Custom Field that disappears from the JSON —
so the stale rows + DB columns linger. This removes them. Idempotent: a no-op once they're gone.
"""
import frappe

_STALE = (
	"CRM Lead-custom_program_lifecycle_stage",
	"CRM Lead-custom_lsq_lead_stage_legacy",
)


def execute():
	for name in _STALE:
		if frappe.db.exists("Custom Field", name):
			frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
