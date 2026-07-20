# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
import frappe
from frappe.model.document import Document


class CRMLeadAPIMapping(Document):
	def validate(self):
		# One contract per login is the DB's job: partner_user keeps its unique, and a blank stores as
		# NULL, which a unique index does not collide. Only the internal case needs code.
		self._one_internal_contract_per_grain()

	def _one_internal_contract_per_grain(self):
		"""An INTERNAL contract resolves BY GRAIN, so a second internal one on the same grain is ambiguous.

		Keyed on is_internal, not on "has no login": a source contract (Facebook, intake) also carries no
		partner_user, but it is resolved by an explicit link from its source and never by grain — so two of
		them, or one beside an internal contract, are not ambiguous at all."""
		if not self.enabled or not self.is_internal:
			return
		clash = frappe.db.get_value(
			"CRM Lead API Mapping",
			{
				"is_internal": 1,
				"enabled": 1,
				"vertical": self.vertical,
				"crm_group": self.crm_group or "",
				"program": self.program or "",
				"name": ["!=", self.name or ""],
			},
			"name",
		)
		if clash:
			frappe.throw(
				frappe._("An enabled internal contract for this grain already exists: {0}.").format(clash),
				title=frappe._("Grain already has an internal contract"),
			)
