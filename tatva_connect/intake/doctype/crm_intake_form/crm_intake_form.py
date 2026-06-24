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
		self._validate_targets()

	def _validate_targets(self):
		"""Fail-closed mapping contract: every mapping's target_field MUST exist on the
		doctype its target_table resolves to (lead = CRM Lead; plan/care/lab/drug via the
		_TABLE child map). `note` is a free-text Note title, so it is not field-checked.

		Read from LIVE meta (frappe.get_meta) — never a baked field list — so a renamed or
		removed field is caught and the bad field is named. Only blocks on SAVE; the format
		migration runs on after_migrate and never goes through this hook."""
		for m in self.mappings:
			table = (m.target_table or "").strip()
			field = (m.target_field or "").strip()
			if not table or not field:
				frappe.throw(
					_("Each mapping needs a Target Table and a Target Field (source '{0}').").format(
						m.source_field or "?"
					),
					title=_("Incomplete Mapping"),
				)
			if table == "note":
				continue
			dt = target_doctype(table)
			if not dt:
				frappe.throw(
					_("Unknown Target Table '{0}' (source '{1}').").format(table, m.source_field or "?"),
					title=_("Invalid Target"),
				)
			if not frappe.get_meta(dt).has_field(field):
				frappe.throw(
					_("Target field '{0}' does not exist on {1} (source '{2}').").format(
						field, dt, m.source_field or "?"
					),
					title=_("Invalid Target Field"),
				)
