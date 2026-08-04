# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from tatva_connect.api._base import is_writable
from tatva_connect.api.partner import ROUTING_FIELDS


class CRMLeadAPIField(Document):
	def validate(self):
		self._fieldname_resolves_against_the_section()
		self._one_row_per_field()
		self._key_addresses_its_own_field()

	def _fieldname_resolves_against_the_section(self):
		"""What `fieldname` means is the section's answer, not this row's. A key-value section addresses a
		ROW by it, so it is an identity and no column need exist; every other section resolves it against
		its target's meta, where a name that is not a column can reach no value.

		A MULTI-VALUE field is the second such case: its selections hang off the lead
		(`tatva_connect.lead.multi_value`) because a field taking many values has no column that could
		hold them, so the fieldname names the field and the picklist category and nothing else. Without
		this the row could not be saved at all once the box was ticked."""
		if not (self.section and self.fieldname):
			return  # reqd catches both
		if self.is_multi_value:
			return
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

	def _one_row_per_field(self):
		"""ONE field, ONE catalog row. A second row for the same (section, fieldname) is a second brain: a
		contract ticks one of the two keys, and which one it happens to hold decides whether the field works
		at all. That is exactly how `lead:substage` sat beside `lead:custom_substage` and disabled stage on
		three of four partner contracts without anything going red."""
		if not (self.section and self.fieldname):
			return  # reqd catches both
		twin = frappe.db.get_value(
			self.doctype,
			{"section": self.section, "fieldname": self.fieldname, "name": ("!=", self.name or "")},
			"name",
		)
		if twin:
			frappe.throw(
				frappe._("{0} already catalogues {1} in section {2}. A field has one row, and a contract "
				         "ticks that row; a second row is a key that grants a different answer.").format(
					twin, self.fieldname, self.section
				),
				title=frappe._("Field Already Catalogued"),
			)

	def _key_addresses_its_own_field(self):
		"""A WRITABLE row's `field_key` must be exactly `<section>:<fieldname>`, because the partner API
		splits the key back apart to find the column (`api/partner.py::_split_keys`) rather than reading
		this row's `fieldname`. A key whose suffix is not its fieldname therefore collects into a column
		that does not exist: the contract lists the field, `lead_schema` omits it, and every value sent
		through it is dropped with a 200.

		The carve-out is not a list kept here — it is the two sets `_build_catalog` itself skips before any
		key is split. A routing field is forced from entitlement and a reserved field is OUTPUT_ONLY, so
		neither reaches `_split_keys` and both may keep a friendlier key (`lead:product_line`,
		`lead:lead_id`). Ask the brain; never restate what it decides."""
		if not (self.section and self.fieldname and self.field_key):
			return  # reqd catches these
		if self.fieldname in ROUTING_FIELDS or not is_writable(self.fieldname):
			return  # never reaches _split_keys — _build_catalog drops it before the key is split
		expected = f"{self.section}:{self.fieldname}"
		if self.field_key == expected:
			return
		frappe.throw(
			frappe._("Field Key must be {0}. The partner API reads the column out of the key itself, so "
			         "{1} would collect into a column named {2}, which does not exist — every value sent "
			         "through it would be dropped without an error.").format(
				expected, self.field_key, self.field_key.partition(":")[2] or self.field_key
			),
			title=frappe._("Field Key Does Not Address Its Field"),
		)
