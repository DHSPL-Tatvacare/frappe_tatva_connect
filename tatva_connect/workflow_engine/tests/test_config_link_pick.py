# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every canvas config Link is picked through one server-declared `pick`, and what a grain picker offers is exactly what publish accepts."""

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.taxonomy import grain
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.workflow_engine import registry
from tatva_connect.workflow_engine.tests import fixtures as fx

_FOREIGN = GRAINS[0]
_MARKER = "WF Pick Probe"


def _offered(doctype, txt, grain_):
	return [row[0] for row in registry.grain_link_query(doctype, txt, "name", 0, 50, grain_)]


def _pool_field():
	return next(f for f in registry.config_fields("Distribute") if f["name"] == "assignment_rule")


class TestConfigLinkPick(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()

	def setUp(self):
		fx.roll_back_pools(self)
		self.addCleanup(frappe.set_user, "Administrator")
		self.home = fx.make_pool([{"user": "Administrator"}], rule="Round Robin")
		self.any_program = fx.make_pool([{"user": "Administrator"}], rule="Round Robin", grain_program=None)
		self.away = fx.make_pool(
			[{"user": "Administrator"}], rule="Round Robin",
			grain_vertical=_FOREIGN["vertical"], grain_group=_FOREIGN["group"], grain_program=_FOREIGN["program"],
		)

	def test_every_config_link_carries_a_pick_derived_from_its_target(self):
		"""The oracle is `grain.columns`, the schema brain — never a list of doctypes typed here."""
		links = [f for t in registry.node_types() for f in t["config"] if f.get("type") == "Link"]
		self.assertTrue(links)
		for field in links:
			with self.subTest(field=field["name"], link=field["link"]):
				if field.get("scope"):
					query = registry.ENTITLED_USER_QUERY
				elif any(grain.columns(field["link"])):
					query = registry.GRAIN_LINK_QUERY
				else:
					query = None
				self.assertEqual(field["pick"], {"kind": "link", "target": field["link"], "query": query})
				self.assertNotIn("grain_scoped", field)

	def test_the_pool_picker_offers_its_grain_and_a_rule_open_on_an_axis(self):
		offered = _offered("Assignment Rule", "WF Pool", fx.GRAIN)
		self.assertIn(self.home.name, offered)
		self.assertIn(self.any_program.name, offered, "a blank program on a rule means every program")
		self.assertNotIn(self.away.name, offered)

	def test_a_disabled_pool_is_hidden_as_frappes_own_search_hides_it(self):
		disabled = fx.make_pool([{"user": "Administrator"}], rule="Round Robin", disabled=1)
		self.assertNotIn(disabled.name, _offered("Assignment Rule", "WF Pool", fx.GRAIN))

	def test_what_the_pool_picker_offers_publish_accepts_and_what_it_hides_publish_refuses(self):
		check, field, context = registry.FIELD_TYPES["Link"]["check"], _pool_field(), {"grain": fx.GRAIN}
		for name in (self.home.name, self.any_program.name):
			with self.subTest(offered=name):
				self.assertEqual(check(name, field, {}, context), [])
		self.assertTrue(check(self.away.name, field, {}, context), "a pool from another grain published green")

	def test_a_saved_value_outside_the_grain_is_still_answered_by_its_key(self):
		"""A closed picker asks by its saved key; unanswered, the author sees a raw key instead of a title."""
		self.assertEqual(_offered("Assignment Rule", self.away.name, fx.GRAIN)[0], self.away.name)

	def test_the_lead_picker_is_scoped_by_the_leads_own_grain_columns(self):
		home = fx.make_lead(first_name=_MARKER)
		away = fx.make_lead(
			first_name=_MARKER, custom_vertical=_FOREIGN["vertical"], custom_group=_FOREIGN["group"],
			custom_current_program=_FOREIGN["program"],
		)
		offered = _offered("CRM Lead", _MARKER, fx.GRAIN)
		self.assertIn(home.name, offered)
		self.assertNotIn(away.name, offered)

	def test_a_caller_who_cannot_read_workflows_is_refused(self):
		frappe.set_user(fx.make_user("wf-pick-outsider@example.invalid"))
		with self.assertRaises(frappe.PermissionError):
			registry.grain_link_query("Assignment Rule", "", "name", 0, 20, fx.GRAIN)
		with self.assertRaises(frappe.PermissionError):
			registry.entitled_user_query("User", "", "name", 0, 20, fx.GRAIN)
