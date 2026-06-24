"""Repoint Nivolumab nivo_dosage/nivo_indication CRM Intake Field Map rows from the wrong `plan` child to the `drug` child where the fields live (data half only); guarded, idempotent."""
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
		# Per-row guard via the filter above: only rows still pointing at `plan` are selected, so a second run is a no-op.
		frappe.db.set_value(_DT, name, "target_table", "drug", update_modified=False)

	if rows:
		frappe.db.commit()
