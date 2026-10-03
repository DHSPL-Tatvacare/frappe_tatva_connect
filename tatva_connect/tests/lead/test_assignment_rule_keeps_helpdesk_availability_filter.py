# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Lock (A.20, CI4): the Assignment Rule controller Frappe resolves keeps Helpdesk's availability filter in its chain whenever helpdesk is importable."""

from frappe.automation.doctype.assignment_rule.assignment_rule import AssignmentRule
from frappe.model.base_document import get_controller
from frappe.tests import UnitTestCase


def _helpdesk_rule():
	try:
		from helpdesk.overrides.assignment_rule import HelpdeskAssignmentRule
	except ImportError:
		return None
	return HelpdeskAssignmentRule


class TestAssignmentRuleKeepsHelpdeskAvailabilityFilter(UnitTestCase):
	def test_the_resolved_controller_extends_helpdesks_rule_when_helpdesk_is_importable(self):
		controller = get_controller("Assignment Rule")
		expected = _helpdesk_rule() or AssignmentRule
		self.assertTrue(issubclass(controller, expected), f"{controller.__mro__} does not extend {expected}")
