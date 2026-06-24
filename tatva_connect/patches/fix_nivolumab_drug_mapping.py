"""Fix the live Nivolumab mapping data-loss: repoint its dosage/indication targets
from the (wrong) `plan` child to the `drug` child where those fields actually live.

The Nivolumab `CRM Intake Field Map` rows map `nivo_dosage` / `nivo_indication` to
`target_table='plan'`, but both fields live on `custom_drug_program_profile` (the
`drug` child). Under the old fold a `plan`-targeted field that isn't on the plan child
was SILENTLY dropped (the data-loss bug); under the Phase-B `validate()` it now FAILS
(every target field must exist on its child) and would block the form from saving.

This corrects ONLY the mapping rows. It is the DATA half of the Nivolumab cleanup:
it does NOT repoint the live Web Form's doc_type and does NOT drop any submission table
(Invariant 14 + "never break the public URL"). The full per-form-doctype re-platform of
the existing Nivolumab form is a documented MANUAL runbook step, not an auto-migration.

Idempotent + guarded: each row is repointed only while it still points at `plan`, so a
re-run is a no-op once corrected, and the WHERE clause matches nothing on a DB that never
had these legacy rows (fresh install / already-fixed). Format-only target rename — values
on `target_field` are untouched.
"""
import frappe

_DT = "CRM Intake Field Map"

# The drug-program child fields that were mis-mapped to the plan child.
_DRUG_FIELDS = ("nivo_dosage", "nivo_indication")


def execute():
	if not frappe.db.table_exists(_DT):
		return

	rows = frappe.get_all(
		_DT,
		filters={"target_field": ("in", _DRUG_FIELDS), "target_table": "plan"},
		pluck="name",
	)
	for name in rows:
		# Per-row guard via the filter above: only rows still pointing at `plan` are
		# selected, so this is a no-op on a second run (nothing left to repoint).
		frappe.db.set_value(_DT, name, "target_table", "drug", update_modified=False)

	if rows:
		frappe.db.commit()
