"""Re-index CRM Lead Screening Answer on the question's digest rather than the question text.

The first indexes were built on `question`, which held a catalog fieldname when a screening question was
expected to be declared and mapped. Questions are now kept exactly as asked, so `question` holds the key
Meta sent, which is unbounded: 292 characters today and nothing stopping a longer one. A text index needs
a prefix length, and any prefix is a number a future question can exceed, at which point two different
questions share one index entry in silence. `question_hash` is fixed width whatever it is given.

The text indexes they replace are dropped before the sync, by drop_screening_text_indexes, because a
column cannot widen to TEXT while a full-length index covers it. Declares the end state; a no-op twice.
The superseded patch is left applied and untouched, per the rule that an applied patch is dead.
"""
import frappe

from tatva_connect.patches import _schema

_DOCTYPE = "CRM Lead Screening Answer"
_TABLE = "tabCRM Lead Screening Answer"
_ADD = (
	("ix_parent_qhash", "`parent`, `question_hash`"),
	("ix_qhash_value", "`question_hash`, `value`(64)"),
)


def execute():
	if not frappe.db.table_exists(_DOCTYPE):
		return
	# has_column takes a DOCTYPE and has_index a TABLE NAME; they are not interchangeable.
	if not frappe.db.has_column(_DOCTYPE, "question_hash"):
		return  # the doctype JSON has not synced yet; a later migrate re-runs nothing, so this is a no-op
	for name, columns in _ADD:
		if not frappe.db.has_index(_TABLE, name):
			_schema.ddl(f"ALTER TABLE `{_TABLE}` ADD INDEX `{name}` ({columns})", _TABLE)  # sqli-ok: constant identifiers only, no user value reaches this string
