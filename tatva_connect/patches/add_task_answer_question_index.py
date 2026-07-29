# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Index every activity key-value table on (question, parent) — the Smart View's one non-seek read.

TWO READS, TWO INDEXES. `ix_parent_fieldname` (parent, fieldname) already serves the FORM: *"every answer
belonging to THIS task"*, which knows the parent. The Smart View asks the mirror question — *"this one
field's answer, for every task in the list"* — which knows only the QUESTION, and an index leading with
`parent` cannot be seeked by a non-leading column. Measured on the bench before this patch:

    type=ALL  possible_keys=None  key=None  rows=20,988      <- a full scan, once per key-value column

and the count query repeated every join, so a worklist showing a dozen extra-question columns paid that
scan two dozen times per page. The lead side never had this problem: `add_screening_answer_indexes` gave
it a question-leading index from the start. This is that same shape, on the table the activity worklist
actually reads. Same question, same answer, second table.

The parent column rides SECOND because the composer partitions by it: with both in one index the ranked
sub-select can seek the question and walk its parents in order, without a filesort.

WHICH TABLE IS NOT NAMED HERE. The key-value home is operator data — a `CRM Task Section` row declares
`target_doctype` and `row_key_field` — so this patch READS the declaration rather than restating it. A
second copy of "where answers live" is exactly the kind of drift the one-brain rule exists to stop.

SKIP-UNTIL-READY. `CRM Task Section` rows are written by `section_seed.ensure_rows` on **after_migrate**,
which runs AFTER post-model-sync patches. On a site mid-upgrade the declaration may not exist yet, so
this no-ops rather than throwing — and `schema_setup._STEPS` (which runs inside after_migrate, post-seed)
lands the index on that same migrate. That ordering trap has aborted a real migrate before.

`frappe.db.add_index` and NOT `patches/_schema.ddl`: that door exists because a raw ALTER leaves frappe's
cached COLUMN list stale (`database.py:1334`), and an index changes no columns. The DDL lock forbids
`sql_ddl`/`rename_column`/`rename_field` and not this. Its sibling `add_screening_answer_indexes` DOES use
a raw ALTER, but only because it needs a prefix length (`value`(64)) on a text column, which `add_index`
cannot express; both columns here are varchar(140) — about 1,120 bytes under utf8mb4, comfortably inside
MariaDB's 3,072-byte key limit — so no prefix is needed and the native door applies.

Declares the end state; a no-op the second time.
"""
import frappe

INDEX_NAME = "ix_answer_question_parent"


def key_value_targets():
	"""(doctype, [question_column, 'parent']) for EVERY key-value activity section — empty while the
	declaration has not been seeded yet. Read from the section rows — never named in code, and never the
	first row alone: the lead snapshot is a second key-value section and a Smart View reads it the same way."""
	return [
		(row.target_doctype, [row.row_key_field, "parent"])
		for row in frappe.get_all(
			"CRM Task Section",
			filters={"is_key_value": 1},
			fields=["target_doctype", "row_key_field"],
			order_by="display_order",
		)
		if row.target_doctype and row.row_key_field
	]


def execute():
	for doctype, columns in key_value_targets():
		if not frappe.db.table_exists(doctype):
			continue
		if frappe.db.has_index(f"tab{doctype}", INDEX_NAME):
			continue
		try:
			frappe.db.add_index(doctype, columns, INDEX_NAME)
		except Exception:
			frappe.log_error(title=f"{doctype} question index failed", message=frappe.get_traceback())
