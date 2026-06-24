# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.intake.intake import target_doctype
from tatva_connect.taxonomy.normalize import normalize_field


class CRMIntakeForm(Document):
	def validate(self):
		# M-2: normalize the form name (the display key) so variants never fork.
		normalize_field(self, "form_name")
		self._validate_fields()
		self._validate_targets()
		self._validate_phone_mapping()

	def _validate_fields(self):
		"""A Select field needs its option list (one per line) — else the published form
		renders an empty, unusable dropdown."""
		for m in self.mappings:
			if (m.get("fieldtype") or "Data").strip() == "Select" and not (m.get("options") or "").strip():
				frappe.throw(
					_("Field '{0}' is a Select but has no Options (one value per line).").format(
						m.source_field or "?"
					),
					title=_("Select Needs Options"),
				)

	def _validate_targets(self):
		"""Fail-closed mapping contract: a row that DECLARES a target_table must point at a
		field that exists on the doctype it resolves to (lead = CRM Lead; plan/care/lab/drug via
		the _TABLE child map; `note` = a free-text Note title, not field-checked). A row with NO
		target_table is a web-form-only input or a layout field (Section/Column Break, HTML) and
		lands nothing on the lead — so it is not field-checked.

		Read from LIVE meta (frappe.get_meta) — never a baked field list — so a renamed or
		removed field is caught and the bad field is named. Only blocks on SAVE."""
		for m in self.mappings:
			table = (m.target_table or "").strip()
			field = (m.target_field or "").strip()
			if not table:
				continue  # unmapped input / layout field — nothing lands on the lead
			if not field:
				frappe.throw(
					_("Field '{0}' maps to '{1}' but has no Target Field.").format(
						m.source_field or "?", table
					),
					title=_("Incomplete Mapping"),
				)
			if table == "note":
				continue
			dt = target_doctype(table)
			if not dt:
				frappe.throw(
					_("Unknown Target Table '{0}' (field '{1}').").format(table, m.source_field or "?"),
					title=_("Invalid Target"),
				)
			if not frappe.get_meta(dt).has_field(field):
				frappe.throw(
					_("Target field '{0}' does not exist on {1} (field '{2}').").format(
						field, dt, m.source_field or "?"
					),
					title=_("Invalid Target Field"),
				)

	def _validate_phone_mapping(self):
		"""Fail-closed: the lead is keyed and deduped on phone, so an ENABLED form with fields
		MUST have exactly one field mapped to lead -> mobile_no (the operator declares WHICH field
		is the phone — nothing is hardcoded). A draft (disabled) or empty form may be saved while
		still being built; it just can't go live without a phone."""
		if not self.enabled or not self.mappings:
			return
		phone_maps = [
			m
			for m in self.mappings
			if (m.target_table or "").strip() == "lead" and (m.target_field or "").strip() == "mobile_no"
		]
		if len(phone_maps) != 1:
			frappe.throw(
				_(
					"Exactly one field must map to lead → Mobile No (the patient's phone) — found {0}. "
					"The lead is deduped on phone, so an enabled form needs one."
				).format(len(phone_maps)),
				title=_("Phone Mapping Required"),
			)
