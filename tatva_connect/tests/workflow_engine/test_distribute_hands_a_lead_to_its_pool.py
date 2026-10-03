# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Distribute hands a lead nobody holds to its pool when `only_when` matches, and leaves by `nobody` when it cannot."""

from unittest.mock import patch

import frappe
from frappe.desk.form import assign_to
from frappe.tests import set_user

from tatva_connect.tests.workflow_engine import fixtures as fx
from tatva_connect.workflow_engine.tests.fixtures import make_lead

_DIABETES = {
	"type": "rule", "field": "crm_lead.custom_acquisition_profile.utm_disease",
	"operator": "contains", "value": "diabetes",
}


def _lead(disease=None):
	return make_lead(custom_acquisition_profile=[{"utm_disease": disease}] if disease else [])


def _holders(lead):
	return frappe.get_all(
		"ToDo", filters={"reference_type": "CRM Lead", "reference_name": lead.name, "status": "Open"},
		fields=["allocated_to", "assignment_rule"],
	)


class TestDistributeHandsALeadToItsPool(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.a, cls.b = fx.rep("distribute-a"), fx.rep("distribute-b")
		cls.rule = fx.pool([(cls.a, 1, None), (cls.b, 1, None)])
		cls.nobody_free = fx.pool([(cls.a, 1, None, {"paused": 1}), (cls.b, 1, None, {"paused": 1})])
		cls.matching, cls.partner_lead, cls.held = (_lead("Type 2 Diabetes") for _ in range(3))
		cls.outside = [_lead("PCOS"), _lead()]
		cls.plain = _lead()
		cls.outsider, cls.partner = fx.rep("distribute-outsider"), fx.make_user(f"distribute-partner-{frappe.generate_hash(length=6)}@example.invalid")

	def test_a_lead_matching_only_when_is_drawn_from_the_pool(self):
		output, user, _opens = fx.distribute(self.rule, self.matching, _DIABETES)
		self.assertEqual(output, "assigned")
		self.assertIn(user, (self.a, self.b))
		self.assertEqual(_holders(self.matching), [{"allocated_to": user, "assignment_rule": self.rule.name}])

	def test_a_lead_outside_only_when_leaves_by_nobody_untouched(self):
		for lead in self.outside:
			with self.subTest(lead=lead.name):
				self.assertEqual(fx.distribute(self.rule, lead, _DIABETES), ("nobody", None, None))
				self.assertEqual(_holders(lead), [])

	def test_a_pool_without_shifts_where_nobody_can_take_it_leaves_by_nobody_never_closed(self):
		self.assertEqual(fx.distribute(self.nobody_free, self.plain), ("nobody", None, None))
		self.assertEqual(_holders(self.plain), [])

	def test_a_lead_held_by_someone_outside_the_grain_keeps_them(self):
		"""The node made no pick, so there is nothing of its own to refuse."""
		assign_to.add({"doctype": "CRM Lead", "name": self.held.name, "assign_to": [self.outsider]})
		self.assertEqual(fx.distribute(self.rule, self.held)[:2], ("assigned", self.outsider))

	def test_a_lead_saved_in_a_partners_session_is_still_distributed(self):
		"""A partner's own save runs the flow; frappe's `_add` then shares the lead, which the partner has no right to do."""
		with set_user(self.partner), patch.dict(frappe.flags, {"in_workflow": True}):
			output, user, _opens = fx.distribute(self.rule, self.partner_lead, _DIABETES)
		self.assertEqual(output, "assigned")
		self.assertIn(user, (self.a, self.b))
