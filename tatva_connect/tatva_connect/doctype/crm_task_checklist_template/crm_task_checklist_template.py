# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CRMTaskChecklistTemplate(Document):
	def validate(self):
		# Two templates with the IDENTICAL (task_type, vertical, group, program)
		# quad are equally specific -> ambiguous which checklist applies. Block the
		# duplicate at the source so resolution stays deterministic. Compare in
		# Python (not a filtered exists) so blank Link axes — which the DB may store
		# as NULL OR "" — match uniformly. Mirrors the routing dup-guards.
		def _key(d):
			return ((d.task_type or ""), (d.vertical or ""), (d.psp_group or ""), (d.program or ""))

		mine = _key(self)
		for other in frappe.get_all(
			"CRM Task Checklist Template",
			filters={"name": ["!=", self.name or ""]},
			fields=["name", "task_type", "vertical", "psp_group", "program"],
		):
			if _key(other) == mine:
				frappe.throw(
					_(
						"A checklist template for the same Task Type / Product Line / Group / "
						"Program already exists ({0}). Each combination must be unique."
					).format(other.name),
					title=_("Duplicate checklist template"),
				)
