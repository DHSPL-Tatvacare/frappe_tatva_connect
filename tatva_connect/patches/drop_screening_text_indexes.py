"""Drop the screening answer's text indexes BEFORE the model sync widens the column they sit on.

`question` was a Data column holding a catalog fieldname. A screening question is now kept exactly as
asked, so the column becomes Small Text, and MariaDB cannot index a TEXT column without a prefix length:
`ALTER TABLE ... MODIFY question text` fails with "Specified key was too long" while `ix_parent_question`
covers the whole column. The indexes must therefore go first, which is why this runs pre_model_sync.

The hash indexes that replace them are added in reindex_screening_answers_by_hash, after the sync has
created the column they read. Declares the end state; a no-op the second time.
"""
import frappe

from tatva_connect.patches import _schema

_TABLE = "tabCRM Lead Screening Answer"
_INDEXES = ("ix_parent_question", "ix_question_value")


def execute():
	if not frappe.db.table_exists("CRM Lead Screening Answer"):
		return
	for name in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			_schema.ddl(f"ALTER TABLE `{_TABLE}` DROP INDEX `{name}`", _TABLE)  # sqli-ok: constant identifiers only, no user value reaches this string
