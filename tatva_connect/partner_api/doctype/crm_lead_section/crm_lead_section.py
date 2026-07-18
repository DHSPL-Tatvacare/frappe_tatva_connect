# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One row per lead section — the ONE home of its table, its target, its row key and its title.

`sql_source` is DERIVED here and stored nowhere — it was a column on all 607 catalog rows and 29 of them
were born blank, because a value copied onto every row is a value that has to be remembered on every row.
Which row a multi-row child shows is read STRUCTURALLY off the section's `row_key_field`, not encoded into
a pick string, so there is one contract and nothing left to rot.
"""
import frappe
from frappe.model.document import Document

LEAD_DOCTYPE = "CRM Lead"


def sql_source(section):
	"""Where this section's columns physically live. Derived — a stored copy is a second brain."""
	return "child" if section.get("child_table_field") else "parent"


class CRMLeadSection(Document):
	def validate(self):
		self._multi_row_needs_a_row_key()
		if not self.target_doctype:
			return  # reqd catches it, and every check below reads the target's meta
		self._row_key_is_a_real_column()
		self._child_table_reaches_the_target()
		self._a_section_with_no_child_table_is_the_lead()

	def _multi_row_needs_a_row_key(self):
		if self.is_multi_row and not self.row_key_field:
			frappe.throw(
				frappe._("A multi-row section needs a Row Key Field: without one no row has an address, so every write lands on the same row."),
				title=frappe._("Row Key Field required"),
			)

	def _row_key_is_a_real_column(self):
		if not self.row_key_field:
			return
		if not frappe.get_meta(self.target_doctype).get_field(self.row_key_field):
			frappe.throw(
				frappe._("{0} is not a field of {1}, so it can address no row.").format(self.row_key_field, self.target_doctype),
				title=frappe._("Unknown Row Key Field"),
			)

	def _child_table_reaches_the_target(self):
		if not self.child_table_field:
			return
		field = frappe.get_meta(LEAD_DOCTYPE).get_field(self.child_table_field)
		if not field or field.fieldtype != "Table" or field.options != self.target_doctype:
			frappe.throw(
				frappe._("{0} is not a Table field on {1} holding {2} rows.").format(self.child_table_field, LEAD_DOCTYPE, self.target_doctype),
				title=frappe._("Child Table Field does not reach the target"),
			)

	def _a_section_with_no_child_table_is_the_lead(self):
		if not self.child_table_field and self.target_doctype != LEAD_DOCTYPE:
			frappe.throw(
				frappe._("A section with no Child Table Field is the lead row itself, so its target must be {0}.").format(LEAD_DOCTYPE),
				title=frappe._("Child Table Field required"),
			)
