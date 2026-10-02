# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The router's cached trigger list follows every workflow change at once: activated is in, suspended or deleted is out."""
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import triggers
from tatva_connect.workflow_engine.tests import fixtures

_WF = "trigger-cache-probe"


def _listed():
	return [w.name for w in triggers.active_triggers("CRM Lead", "Created")]


class TestTheTriggerCacheFollowsTheWorkflow(FrappeTestCase):
	def setUp(self):
		fixtures.purge(_WF)
		self.addCleanup(fixtures.purge, _WF)

	def test_activating_suspending_and_deleting_each_take_effect_on_the_next_read(self):
		_listed()  # warm the cache first, so each assertion proves the clear rather than a cold read
		workflow = fixtures.make_workflow(_WF, [fixtures.trigger(to="end"), fixtures.node("end", "Terminal")])
		self.assertIn(workflow.name, _listed(), "an activated workflow is not in the trigger list")

		workflow.reload()
		workflow.apply_transition("Suspended")
		self.assertNotIn(workflow.name, _listed(), "a suspended workflow is still in the trigger list")

		workflow.reload()
		workflow.apply_transition("Active")
		self.assertIn(workflow.name, _listed())

		fixtures.purge(_WF)
		self.assertNotIn(workflow.name, _listed(), "a deleted workflow is still in the trigger list")

	def test_the_list_carries_the_grain_the_router_matches_on(self):
		workflow = fixtures.make_workflow(_WF, [fixtures.trigger(to="end"), fixtures.node("end", "Terminal")])
		row = next(w for w in triggers.active_triggers("CRM Lead", "Created") if w.name == workflow.name)
		self.assertEqual((row.vertical, row.group, row.program), (fixtures.GRAIN["vertical"], fixtures.GRAIN["group"], fixtures.GRAIN["program"]))
