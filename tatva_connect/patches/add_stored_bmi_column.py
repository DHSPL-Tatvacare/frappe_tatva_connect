# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Land the physical column for the stored BMI field.

custom_bmi was a virtual field computed from Height and Weight, so it carried no column. It is now the
STORED field the migration writes the source system's recorded BMI into, and the derived value moved to
custom_computed_bmi, which keeps the formula.

The fixture owns both field definitions — this patch deliberately restates neither. It lands only the
physical column, because fixtures sync AFTER patches on a migrate, so without it the recorded BMI has
nowhere to be written until a second migrate.

Idempotent: the column is added only when information_schema says it is absent, which is also why a fresh
site is a clean no-op once the fixture has built it. Assumes nothing about what ran before.
"""
import frappe

from tatva_connect.patches import _schema

_TABLE = "tabCRM Lead"
_COLUMN = "custom_bmi"


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
	if _column_exists(_TABLE, _COLUMN):
		return
	# ALLOWLIST: raw ADD COLUMN DDL — the fixture cannot land a column before it syncs, and Float is decimal(21,9)
	_schema.ddl(f"ALTER TABLE `{_TABLE}` ADD COLUMN `{_COLUMN}` decimal(21,9) NOT NULL DEFAULT 0", _TABLE)
