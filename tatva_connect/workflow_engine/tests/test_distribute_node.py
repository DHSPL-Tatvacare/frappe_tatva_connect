# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Distribute hands a lead nobody holds to its pool when `only_when` matches, driven through `interpreter._run_verb`."""

from unittest.mock import patch

import frappe
from frappe.desk.form import assign_to
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import context as ctx_build
from tatva_connect.workflow_engine import interpreter, refs
from tatva_connect.workflow_engine.tests import fixtures as fx

_NO_GRAIN = ("", "", "")
_DIABETES = {
	"type": "rule", "field": "crm_lead.custom_acquisition_profile.utm_disease",
	"operator": "contains", "value": "diabetes",
}


class TestDistributeNode(FrappeTestCase):
	def setUp(self):
		fx.roll_back_pools(self)
		self.a, self.b = (fx.make_user(f"distribute-probe-{i}@example.invalid") for i in "ab")
		self.pool = fx.make_pool([{"user": u, "weight": 1} for u in (self.a, self.b)], assigned_by_workflow=1)

	def _lead(self, disease=None):
		return fx.make_lead(custom_acquisition_profile=[{"utm_disease": disease}] if disease else [])

	def _fire(self, lead, axes=_NO_GRAIN, **config):
		node = frappe._dict({
			"node_id": "d", "node_type": "Distribute", "edges": [],
			"config_json": frappe.as_json({"assignment_rule": self.pool.name, **config}),
		})
		state = ctx_build.context_for(lead, {})
		interpreter._run_verb(node, lead.name, lead, state, axes)
		view = state.writing_as("d")
		return view.get(refs.OUTPUT), view.get("d.assigned_to")

	def _holders(self, lead):
		return frappe.get_all(
			"ToDo", filters={"reference_type": "CRM Lead", "reference_name": lead.name, "status": "Open"},
			fields=["allocated_to", "assignment_rule"],
		)

	def test_a_child_section_value_matching_only_when_is_drawn_from_the_pool(self):
		lead = self._lead("Type 2 Diabetes")
		self.assertEqual(self._fire(lead, only_when=_DIABETES), ("assigned", self.a))
		self.assertEqual(self._holders(lead), [{"allocated_to": self.a, "assignment_rule": self.pool.name}])

	def test_a_lead_outside_only_when_leaves_by_nobody_untouched(self):
		for disease in ("PCOS", None):
			with self.subTest(disease=disease):
				lead = self._lead(disease)
				self.assertEqual(self._fire(lead, only_when=_DIABETES), ("nobody", None))
				self.assertEqual(self._holders(lead), [])

	def test_a_held_lead_keeps_its_holder(self):
		lead = self._lead("Type 2 Diabetes")
		assign_to.add({"doctype": "CRM Lead", "name": lead.name, "assign_to": [self.b]})
		self.assertEqual(self._fire(lead), ("assigned", self.b))
		self.assertEqual([h.allocated_to for h in self._holders(lead)], [self.b])

	def test_a_lead_held_by_someone_outside_the_grain_keeps_them(self):
		"""The node made no pick, so there is nothing of its own to refuse."""
		lead = self._lead("Type 2 Diabetes")
		outsider = fx.make_user("distribute-probe-outsider@example.invalid")
		assign_to.add({"doctype": "CRM Lead", "name": lead.name, "assign_to": [outsider]})
		self.assertEqual(self._fire(lead, axes=fx.AXES), ("assigned", outsider))

	def test_a_pool_with_no_one_able_to_take_it_leaves_by_nobody(self):
		for row in self.pool.weighted_users:
			row.paused = 1
		self.pool.save(ignore_permissions=True)
		lead = self._lead()
		self.assertEqual(self._fire(lead), ("nobody", None))
		self.assertEqual(self._holders(lead), [])

	def test_a_rolelessly_saved_lead_is_distributed_to_a_rep_who_cannot_read_it_yet(self):
		"""A partner's own save runs the flow; frappe's `_add` then shares the lead, which the partner has no right to do."""
		lead = self._lead("Type 2 Diabetes")
		partner = fx.make_user("distribute-probe-partner@example.invalid")
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user(partner)
		with patch.dict(frappe.flags, {"in_workflow": True}):
			self.assertEqual(self._fire(lead, only_when=_DIABETES), ("assigned", self.a))
