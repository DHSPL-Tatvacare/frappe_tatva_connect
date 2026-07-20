# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CRMLeadAPIField(Document):
	def validate(self):
		self._fieldname_resolves_against_the_section()

	def _fieldname_resolves_against_the_section(self):
		"""What `fieldname` means is the section's answer, not this row's. A key-value section addresses a
		ROW by it, so it is an identity and no column need exist; every other section resolves it against
		its target's meta, where a name that is not a column can reach no value."""
		if not (self.section and self.fieldname):
			return  # reqd catches both
		section = frappe.get_cached_doc("CRM Lead Section", self.section)
		if section.is_key_value or not section.target_doctype:
			return
		if not frappe.get_meta(section.target_doctype).get_field(self.fieldname):
			frappe.throw(
				frappe._("{0} is not a field of {1}, the target of section {2}.").format(
					self.fieldname, section.target_doctype, section.name
				),
				title=frappe._("Unknown Fieldname"),
			)
