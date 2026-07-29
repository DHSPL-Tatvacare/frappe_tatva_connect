"""(parent, fieldname) on CRM Task Lead Snapshot — the same read, the same shape, the second key-value table.

The lead snapshot is one row per lead-sourced declared field per task, so a parent-only lookup filters every
one of that task's snapshot rows, exactly as it did on CRM Task Answer before `ix_parent_fieldname`. A
composite index cannot be declared in a doctype JSON and frappe declares (parent) on every child table
itself, so only the pair is added. Declares the end state, no-op twice; install-app baselines patches.txt
without running it, so schema_setup._STEPS carries this to a fresh site too."""
import frappe

from tatva_connect.patches import _schema

_DOCTYPE = "CRM Task Lead Snapshot"
_TABLE = "tabCRM Task Lead Snapshot"
_INDEX = ("ix_parent_fieldname", "`parent`, `fieldname`")


def execute():
	if not frappe.db.table_exists(_DOCTYPE):
		return
	# has_column takes a DOCTYPE and has_index a TABLE NAME; they are not interchangeable.
	if not frappe.db.has_column(_DOCTYPE, "fieldname"):
		return  # the doctype JSON has not synced yet; the after_migrate schema_setup pass lands it
	name, columns = _INDEX
	if not frappe.db.has_index(_TABLE, name):
		_schema.ddl(f"ALTER TABLE `{_TABLE}` ADD INDEX `{name}` ({columns})", _TABLE)  # sqli-ok: constant identifiers only, no user value reaches this string
