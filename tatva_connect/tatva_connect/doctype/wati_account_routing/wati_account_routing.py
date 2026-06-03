# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class WATIAccountRouting(Document):
	def validate(self):
		# No global default: a rule must scope at least one axis. An all-blank rule
		# would match every lead (score 0) and silently become a catch-all.
		if not (self.vertical or self.psp_group or self.program):
			frappe.throw(
				_(
					"Set at least one of Product Line / Group / Program. An all-blank rule "
					"would act as a global default, which is not allowed."
				),
				title=_("Invalid routing rule"),
			)

		# Two rules with the IDENTICAL (vertical, group, program) triple are equally
		# specific and could both match a lead -> ambiguous routing. Block the
		# duplicate at the source so resolution stays deterministic.
		# Compare in Python (not a filtered exists) so empty Link axes — which the
		# DB may store as NULL OR "" — match uniformly.
		def _triple(d):
			return ((d.vertical or ""), (d.psp_group or ""), (d.program or ""))

		mine = _triple(self)
		for other in frappe.get_all(
			"WATI Account Routing",
			filters={"name": ["!=", self.name or ""]},
			fields=["name", "vertical", "psp_group", "program"],
		):
			if _triple(other) == mine:
				frappe.throw(
					_(
						"A routing rule with the same Product Line / Group / Program already "
						"exists ({0}). Each combination must be unique."
					).format(other.name),
					title=_("Duplicate routing rule"),
				)
