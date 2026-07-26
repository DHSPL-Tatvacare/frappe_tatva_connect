"""(parent, fieldname) on CRM Task Answer — the key-value read is "this task's row for this fieldname",
and a doctype JSON cannot express a composite index. Frappe declares (parent) on every child table itself,
but this is the widest child table in the app (one row per DECLARED field per task, not per task), so a
parent-only lookup still filters every one of that task's answers. Same shape and same reason as
ix_parent_qhash on CRM Lead Screening Answer. Declares the end state, no-op twice; install-app baselines
patches.txt without running it, so schema_setup._STEPS carries this to a fresh site too."""
import frappe

from tatva_connect.patches import _schema

_DOCTYPE = "CRM Task Answer"
_TABLE = "tabCRM Task Answer"
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
