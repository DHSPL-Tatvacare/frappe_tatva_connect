# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Override of core `Assignment Rule`: gate CRM Lead assignment on grain.

A grain-tagged rule (grain_vertical/group/program — fixtures, mandatory only for
CRM Lead) may only fire on a lead whose grain matches every set axis. Stock for
non-CRM-Lead rules and for rules with no grain set."""

from frappe.automation.doctype.assignment_rule.assignment_rule import AssignmentRule


class TatvaAssignmentRule(AssignmentRule):
	def apply_assign(self, doc):
		if self.document_type == "CRM Lead" and not self._lead_grain_matches(doc):
			return False
		return super().apply_assign(doc)

	def _lead_grain_matches(self, doc):
		for rule_field, lead_field in (
			("grain_vertical", "custom_vertical"),
			("grain_group", "custom_group"),
			("grain_program", "custom_current_program"),
		):
			rule_value = self.get(rule_field)
			if rule_value and doc.get(lead_field) != rule_value:
				return False
		return True
