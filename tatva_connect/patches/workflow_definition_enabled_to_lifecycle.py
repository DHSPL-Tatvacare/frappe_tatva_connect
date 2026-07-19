"""Definition lifecycle: the CRM Workflow Definition `enabled` Check becomes the `lifecycle_state` Select
(Draft / Published / Active / Suspended / Archived).

A one-time data migration: at cutover no workflow has ever been Published/Suspended/Archived (those states
did not exist), so the complete correct mapping is enabled=1 -> Active, enabled=0 -> Draft. Runs pre_model_sync
so it reads `enabled` (still present) and pre-creates + populates `lifecycle_state` before the JSON sync adds
it — otherwise sync would default every row to Draft and silently disarm a live automation.

Frappe does NOT auto-drop a field removed from the JSON (see patches.txt), so this patch DROPS `enabled`
itself through the one DDL door. That does two things: it leaves no orphan column, AND it makes the guard
(`if "enabled" not in columns: return`) fire on any later run — so this patch is idempotent and safe on the
mandated "migrate twice" replay, never re-clobbering the operator lifecycle transitions made after cutover.
Fresh install: the table (or the `enabled` column) does not exist, so it no-ops."""
import frappe

from tatva_connect.patches import _schema

_TABLE = "tabCRM Workflow Definition"


def execute():
	if not frappe.db.table_exists("CRM Workflow Definition"):
		return  # fresh install before the doctype exists — the JSON ships lifecycle_state, nothing to map
	columns = frappe.db.get_table_columns("CRM Workflow Definition")
	if "enabled" not in columns:
		return  # already migrated (this patch dropped `enabled`) — no source to read, no-op
	if "lifecycle_state" not in columns:
		_schema.ddl(
			f"ALTER TABLE `{_TABLE}` ADD COLUMN `lifecycle_state` varchar(140) DEFAULT 'Draft'",
			_TABLE,
		)
	frappe.db.sql(
		f"UPDATE `{_TABLE}` SET `lifecycle_state` = CASE WHEN `enabled` = 1 THEN 'Active' ELSE 'Draft' END"
	)
	_schema.ddl(f"ALTER TABLE `{_TABLE}` DROP COLUMN `enabled`", _TABLE)  # no orphan + makes the guard idempotent
	frappe.db.commit()
