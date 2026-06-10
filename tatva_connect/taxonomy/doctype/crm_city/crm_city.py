# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.taxonomy.normalize import normalize_field


class CRMCity(Document):
	def validate(self):
		# M-2: normalize display values so variants converge.
		normalize_field(self, "city_name")
		normalize_field(self, "state")
		# Composite-key safety (§A1 b): a blank axis canonicalizes to None so the
		# `format:{city_name}::{state}` name is built consistently.
		if not (self.state or "").strip():
			self.state = None
		# Composite-key safety (§A1 c): `format:` only fires at INSERT, so re-check the
		# (city_name, state) tuple on every save to block an edit-time in-place duplicate.
		dupe = frappe.db.get_value(
			"CRM City",
			{
				"city_name": self.city_name,
				"state": self.state,
				"name": ["!=", self.name or ""],
			},
			"name",
		)
		if dupe:
			frappe.throw(
				_("A city {0} already exists in {1} ({2}).").format(
					self.city_name, self.state or "(no state)", dupe
				),
				title=_("Duplicate city"),
			)
