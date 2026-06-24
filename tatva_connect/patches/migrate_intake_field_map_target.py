"""Split the legacy free-text `target` on CRM Intake Field Map rows into the structured
(target_table, target_field) pair.

The mapping contract moved from a single `"prefix:field"` Data column to a discoverable,
validated pair: `target_table` (Select) + `target_field` (Data). This carries existing rows
forward so no mapping is lost when the legacy `target` column is dropped on field-sync.

FORMAT-ONLY: `"plan:nivo_dosage"` -> table="plan", field="nivo_dosage". It does NOT remap
`plan` -> `drug` (the Nivolumab dosage/indication fields actually live on the drug child) —
that data correction is Phase-D, done deliberately on the real form, not here.

Runs in patches.txt [pre_model_sync]: the split must happen BEFORE the new JSON syncs and
drops `target`, so we add the two columns physically, populate them, then let the JSON
formalise them (same shape as the whatsapp/telephony spine renames). Idempotent + guarded —
a no-op once the legacy column is gone (already migrated / fresh install), and per-row it
only fills a blank structured pair.
"""
import frappe

_DT = "CRM Intake Field Map"
_TABLE = "tab" + _DT


def _column_exists(table, column):
	"""True if the column physically exists. Read from information_schema, not has_column,
	which can report a stale meta value after a raw DDL change in the same migrate."""
	return bool(
		frappe.db.sql(
			"""SELECT 1 FROM information_schema.COLUMNS
			   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s""",
			(table, column),
		)
	)


def execute():
	if not frappe.db.table_exists(_DT):
		return
	# The legacy column is dropped once the new JSON syncs; nothing to carry after that.
	if not _column_exists(_TABLE, "target"):
		return

	# Ensure the destination columns exist (pre_model_sync runs before the JSON adds them).
	if not _column_exists(_TABLE, "target_table"):
		frappe.db.sql_ddl("ALTER TABLE `{0}` ADD COLUMN `target_table` varchar(140)".format(_TABLE))
	if not _column_exists(_TABLE, "target_field"):
		frappe.db.sql_ddl("ALTER TABLE `{0}` ADD COLUMN `target_field` varchar(140)".format(_TABLE))

	rows = frappe.db.sql(
		"""
		SELECT name, `target`
		FROM `{0}`
		WHERE `target` IS NOT NULL AND `target` != ''
		  AND COALESCE(target_table, '') = ''
		  AND COALESCE(target_field, '') = ''
		""".format(_TABLE),
		as_dict=True,
	)
	for r in rows:
		table, _sep, field = (r.target or "").partition(":")
		table = (table or "").strip()
		field = (field or "").strip()
		if not table:
			continue
		frappe.db.set_value(
			_DT, r.name, {"target_table": table, "target_field": field}, update_modified=False
		)
	frappe.db.commit()
