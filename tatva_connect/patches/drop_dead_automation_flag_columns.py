"""Drop the `can_read`/`can_set` COLUMNS the retired automation allowlist left on the two field catalogs.

The flags went from the doctypes with the allowlist itself, but frappe never drops a column when a field
leaves a doctype, so they survived in MariaDB — invisible to `get_meta` and to the Desk, and reading as a
live permission to anyone who met them in raw SQL. `CRM Task Field.can_set` STAYS (`activity.api
.task_columns` owns it) and so does `can_watch` on both (the dispatcher's diff list). The 2026-08-14
Anaya seed's `can_set` write goes with this, or the next seed run stops on an unknown column.

DDL through `_schema.ddl` (patches.txt rule 1). No schema_setup twin: a fresh site builds these tables
from doctypes that never declared the fields. Idempotent (has_column guard).
"""

import frappe

from tatva_connect.patches import _schema

_DEAD = (
	("CRM Lead API Field", "can_read"),
	("CRM Lead API Field", "can_set"),
	("CRM Task Field", "can_read"),
)


def execute():
	for doctype, fieldname in _DEAD:
		if not frappe.db.table_exists(doctype):
			continue
		if not frappe.db.has_column(doctype, fieldname):
			continue
		table = f"tab{doctype}"
		_schema.ddl(f"ALTER TABLE `{table}` DROP COLUMN `{fieldname}`", table)
