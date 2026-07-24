"""Composite indexes on CRM Lead Screening Answer: (parent, question) backs the Data tab's read of one lead's answers, (question, value) backs the Smart View join and the filter on it. A doctype JSON cannot express a composite index, and the answer is a text column so its half needs a prefix length. Declares the end state; a no-op the second time."""
import frappe

from tatva_connect.patches import _schema

_DOCTYPE = "CRM Lead Screening Answer"
_TABLE = "tabCRM Lead Screening Answer"
_INDEXES = (
	("ix_parent_question", "`parent`, `question`"),
	("ix_question_value", "`question`, `value`(64)"),
)


def execute():
	if not frappe.db.table_exists(_DOCTYPE):
		return
	# Superseded by reindex_screening_answers_by_hash. Once question_hash exists, `question` is unbounded
	# text and a full-length index over (parent, question) exceeds MariaDB's 3072-byte key limit (1071),
	# which aborts the migrate on any site that never applied this line. The hash indexes cover both reads.
	if frappe.db.has_column(_DOCTYPE, "question_hash"):
		return
	for name, columns in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		_schema.ddl(f"ALTER TABLE `{_TABLE}` ADD INDEX `{name}` ({columns})", _TABLE)  # sqli-ok: constant identifiers only, no user value reaches this string
