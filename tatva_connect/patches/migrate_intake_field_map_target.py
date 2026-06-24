"""Split the legacy free-text `target` ("plan:field") on CRM Intake Field Map into the structured (target_table, target_field) pair before the JSON drops it (pre-model-sync, format-only); guarded, idempotent."""
import frappe

_DT = "CRM Intake Field Map"
_TABLE = "tab" + _DT


def _column_exists(table, column):
	"""True if the column physically exists; read from information_schema (has_column can report stale after a raw DDL change in the same migrate)."""
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
