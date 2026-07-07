# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Leg C sign-off - `changed_from_to` criterion operator + rules._one_match branch.

Two layers: (1) the evaluator (rules._one_match) on a hand-built context - real Frappe types, no
mocked verdict; (2) the authoring guard (CRMAutomationRule.validate) - real Frappe throws.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import rules
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

_DT = "CRM Automation Rule"
_LEAD_FIELD = "custom_stage"  # Select on CRM Lead
_GRAIN = GRAINS[0]


def _criterion(field, operator, from_value=None, value=None):
	"""A minimal duck-typed criterion row - rules._one_match only reads .field/.operator/.value/."""
	class _C:
		pass
	c = _C()
	c.field = field
	c.operator = operator
	c.from_value = from_value
	c.value = value
	return c


class TestChangedFromToEvaluator(FrappeTestCase):
	"""The evaluator (rules._one_match). Fail-soft parity: a missing `__before` is a non-match, not a raise."""

	# (a) a clean A->B transition matches from_value=A, value=B.
	def test_match_on_clean_transition(self):
		ctx = {"stage": "B", "stage__before": "A"}
		self.assertTrue(rules._one_match(_criterion("stage", "changed_from_to", "A", "B"), ctx, "Select"))

	# (b) from_value mismatch -> non-match.
	def test_no_match_when_from_value_wrong(self):
		ctx = {"stage": "B", "stage__before": "A"}
		self.assertFalse(rules._one_match(_criterion("stage", "changed_from_to", "X", "B"), ctx, "Select"))

	# (c) value (new) mismatch -> non-match.
	def test_no_match_when_new_value_wrong(self):
		ctx = {"stage": "B", "stage__before": "A"}
		self.assertFalse(rules._one_match(_criterion("stage", "changed_from_to", "A", "Y"), ctx, "Select"))

	# (d) missing `__before` key -> non-match, NO raise (fail-soft parity with the existing operators).
	def test_missing_before_key_is_non_match_not_raise(self):
		ctx = {"stage": "B"}  # no stage__before
		self.assertFalse(rules._one_match(_criterion("stage", "changed_from_to", "A", "B"), ctx, "Select"))


class TestChangedFromToAuthoring(FrappeTestCase):
	"""The authoring guard (CRMAutomationRule.validate). Real Frappe throws.

	TATVA v2 (Task 1): rewritten off trigger_type/watch_doctype/watch_field onto on_doctype/event;
	the operator renamed changed_from_to -> "changed from…to" (Part A). The old restriction to a
	single rule-wide watch_field is dropped in v2 (see CRMAutomationRule._validate_changed_operator)
	- (e) is retargeted to the two v2-real incompleteness/event-gating checks that replace it."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		field_allowlist.seed_watchable("CRM Lead", _LEAD_FIELD)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete(_DT, {"rule_name": ("like", "LegC%")})
		field_allowlist.clear("CRM Lead")

	def _rule_doc(self, criterion, **overrides):
		base = {
			"doctype": _DT,
			"rule_name": f"LegC-{self._testMethodName}",
			"enabled": 0,
			"on_doctype": "CRM Lead",
			"event": "Updated",
			"vertical": _GRAIN["vertical"],
			"group": _GRAIN["group"],
			"program": _GRAIN["program"],
			"criteria": [criterion],
		}
		base.update(overrides)
		return frappe.get_doc(base)

	# (e) `changed from…to` missing its From Value -> validate() raises (v2 has no single
	# watch_field restriction to test instead - any on_doctype meta field is a legal criterion
	# field now, so the incompleteness check is the v2-real replacement for "wrong field").
	def test_changed_from_to_incomplete_criterion_throws(self):
		c = {"field": _LEAD_FIELD, "operator": "changed from…to", "from_value": None, "value": "B"}
		doc = self._rule_doc(c)
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.insert(ignore_permissions=True)

	# (f) `changed from…to` on event=Created -> validate() raises (changed operators need a
	# before-state; Created has none - the v2-real replacement for the old Task-Completed case).
	def test_changed_from_to_on_created_event_throws(self):
		c = {"field": _LEAD_FIELD, "operator": "changed from…to", "from_value": "A", "value": "B"}
		doc = self._rule_doc(c, event="Created")
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.insert(ignore_permissions=True)


if __name__ == "__main__":
	unittest.main()
