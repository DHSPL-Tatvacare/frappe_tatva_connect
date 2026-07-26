"""(parent, document_kind) on CRM Task Document — the multi-row section's latest-by pick reads a task's
rows by kind, and a doctype JSON cannot express a composite index. Frappe declares (parent) on every child
table itself, so that one is left alone. Declares the end state, no-op twice; install-app baselines
patches.txt without running it, so schema_setup._STEPS carries this to a fresh site too."""
import frappe

from tatva_connect.patches import _schema

_DOCTYPE = "CRM Task Document"
_TABLE = "tabCRM Task Document"
_INDEX = ("ix_parent_document_kind", "`parent`, `document_kind`")


def execute():
	if not frappe.db.table_exists(_DOCTYPE):
		return
	# has_column takes a DOCTYPE and has_index a TABLE NAME; they are not interchangeable.
	if not frappe.db.has_column(_DOCTYPE, "document_kind"):
		return  # the doctype JSON has not synced yet; the after_migrate schema_setup pass lands it
	name, columns = _INDEX
	if not frappe.db.has_index(_TABLE, name):
		_schema.ddl(f"ALTER TABLE `{_TABLE}` ADD INDEX `{name}` ({columns})", _TABLE)  # sqli-ok: constant identifiers only, no user value reaches this string
