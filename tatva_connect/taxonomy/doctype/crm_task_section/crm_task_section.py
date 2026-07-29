# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One row per activity section — the ONE home of its table, its target, its row key and its title.

The task twin of CRM Lead Section, in all four of its shapes: the task row itself, a single child row, a
multi-row child, and key-value. A section says only which doctype's columns its fields are — `fieldtype`
and `options` live on CRM Task Type Field, and nothing is restated on a field row.

Key-value is not an alternative to named columns, it is the DEFAULT home. The live seed's slot-targeted
fields are arbitrary per-type named values ("Assessment Date - Physio", "Transaction ID"), so they share
no shape and may not become named columns; one row per fieldname is where they belong.
"""
import frappe
from frappe.model.document import Document

TASK_DOCTYPE = "CRM Task"

# Every field on this doctype that NAMES a column of the target. One list, so a new one is validated by
# having been added here rather than by anyone remembering to write a check for it.
COLUMN_FIELDS = ("row_key_field", "value_field", "label_field", "question_field")

# What a key-value section must name before it can hold anything: where a row's identity and its answer
# each live, and the raw key that identity was derived from. `label_field` is NOT here — an activity field
# is declared on CRM Task Type Field, which already carries its label, so a stored copy is a second brain.
_KEY_VALUE_REQUIRED = ("row_key_field", "value_field", "question_field")


class CRMTaskSection(Document):
	def validate(self):
		self._a_task_section_is_never_multi_row()
		self._multi_row_needs_a_row_key()
		self._key_value_needs_an_address_and_a_value()
		self._key_value_is_not_multi_row()
		if not self.target_doctype:
			return  # reqd catches it, and every check below reads the target's meta
		self._every_named_column_is_real()
		self._child_table_reaches_the_target()
		self._a_section_with_no_child_table_is_the_task()

	def _key_value_needs_an_address_and_a_value(self):
		missing = [f for f in _KEY_VALUE_REQUIRED if not self.get(f)]
		if self.is_key_value and missing:
			frappe.throw(
				frappe._("A key-value section must name every column it uses; missing: {0}. Its rows ARE its fields, so nothing can read one until it knows where the identity, the answer and the raw key each live.").format(", ".join(missing)),
				title=frappe._("Key-value section is incomplete"),
			)

	def _key_value_is_not_multi_row(self):
		if self.is_key_value and self.is_multi_row:
			frappe.throw(
				frappe._("A section is keyed by a field or dated by a row key, never both: a key-value section already holds exactly one row per field."),
				title=frappe._("Key Value and Multi Row are exclusive"),
			)

	def _a_task_section_is_never_multi_row(self):
		"""Unsatisfiable on the task path, so it is refused rather than left to be declared and never built:
		an activity field is addressed by the COLUMN `field_target` names, so a second row has no address and
		no writer could ever reach it. The `documents` section carried this shape for months — 941 rows, and
		`document_kind` set on none of them. A question asked many times per activity is key-value."""
		if self.is_multi_row:
			frappe.throw(
				frappe._("A task section holds one row per task. An activity field is addressed by a column, never by a row key, so a second row could never be written or read — declare the question in a key-value section instead."),
				title=frappe._("Multi Row is not a task-section shape"),
			)

	def _multi_row_needs_a_row_key(self):
		if self.is_multi_row and not self.row_key_field:
			frappe.throw(
				frappe._("A multi-row section needs a Row Key Field: without one no row has an address, so every write lands on the same row."),
				title=frappe._("Row Key Field required"),
			)

	def _every_named_column_is_real(self):
		"""A section pointing at a column which does not exist is a section every consumer reads a None out of."""
		meta = frappe.get_meta(self.target_doctype)
		for field in COLUMN_FIELDS:
			named = self.get(field)
			if named and not meta.get_field(named):
				frappe.throw(
					frappe._("{0} names {1}, which is not a field of {2}.").format(
						self.meta.get_label(field), named, self.target_doctype
					),
					title=frappe._("Unknown column"),
				)

	def _child_table_reaches_the_target(self):
		if not self.child_table_field:
			return
		field = frappe.get_meta(TASK_DOCTYPE).get_field(self.child_table_field)
		if not field or field.fieldtype != "Table" or field.options != self.target_doctype:
			frappe.throw(
				frappe._("{0} is not a Table field on {1} holding {2} rows.").format(self.child_table_field, TASK_DOCTYPE, self.target_doctype),
				title=frappe._("Child Table Field does not reach the target"),
			)

	def _a_section_with_no_child_table_is_the_task(self):
		if not self.child_table_field and self.target_doctype != TASK_DOCTYPE:
			frappe.throw(
				frappe._("A section with no Child Table Field is the task row itself, so its target must be {0}.").format(TASK_DOCTYPE),
				title=frappe._("Child Table Field required"),
			)
