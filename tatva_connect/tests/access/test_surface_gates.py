# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One rule decides every surface: `has_permission(<its doctype>)` AND the surface's operator toggle.

What is asserted:

  * a surface is hidden from a caller who may not read its records, however armed the toggle is;
  * a surface is hidden while its toggle is off, however permitted the caller is;
  * it appears only when both halves pass;
  * Workflows obeys its own surface switch, which is the half it used to be missing entirely;
  * `my_surfaces()["near_me"]` equals `near_me.api._can_access()` for the SAME user in every
    combination — the assertion that stops the one rule and its one home drifting apart;
  * a surface that raises is hidden and the others still answer, because this map rides the boot
    payload and a broken gate must never white-screen the app;
  * Contacts and Organizations obey the SAME two halves as Deals — a contact and an organization exist
    to be sold to, so they ride the Deals liveness answer and their own doctype permission;
  * the whole map stays inside the query budget the plan set, measured rather than reasoned — the budget
    is what proves the two extra surfaces were added for free.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_surface_gates
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access.surfaces import WORKFLOW_SURFACE_SWITCH, my_surfaces
from tatva_connect.near_me import api as near_me_api
from tatva_connect.schema_setup import _ensure_field_map_role

ARMED = "ZZ Surface Armed Line"
UNARMED = "ZZ Surface Unarmed Line"
STRANGER = "zz-surface-stranger@example.test"

# The plan's Performance budget: the whole map costs no more than three indexed reads.
BUDGET_QUERIES = 3


class TestSurfaceGates(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_field_map_role()
		for vertical, deals_enabled in ((ARMED, 1), (UNARMED, 0)):
			if not frappe.db.exists("CRM Vertical", vertical):
				frappe.get_doc({
					"doctype": "CRM Vertical", "vertical_name": vertical, "deals_enabled": deals_enabled,
				}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		if not frappe.db.exists("User", STRANGER):
			frappe.get_doc({
				"doctype": "User", "email": STRANGER, "first_name": "ZZ Surface Stranger",
				"send_welcome_email": 0,
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.delete("User", {"name": STRANGER})
		for vertical in (ARMED, UNARMED):
			if frappe.db.exists("CRM Vertical", vertical):
				frappe.delete_doc("CRM Vertical", vertical, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		# In process, never written to the bench: a written switch survives rollback and leaks into later suites.
		self._live = set()
		enabled = patch("tatva_connect.automation.is_enabled", side_effect=lambda key: key in self._live)
		enabled.start()
		self.addCleanup(enabled.stop)

	# ---- fixtures ----------------------------------------------------------------------------------

	def _arm(self, *live):
		"""Exactly these switches are on; every other automation is dormant, which is how the app ships."""
		self._live = set(live)

	def _entitled_to(self, *verticals):
		"""The caller's entitled region, taken from the one brain — patched here so the region is the only variable."""
		grains = {(vertical, "", "") for vertical in verticals}
		entitled = patch("tatva_connect.access.entitlement.entitled_grains", return_value=grains)
		entitled.start()
		self.addCleanup(entitled.stop)

	def _as_stranger(self):
		"""A real user holding no role — the permission half answered by the permission table, not a literal."""
		frappe.set_user(STRANGER)
		self.addCleanup(frappe.set_user, "Administrator")

	# ---- the rule ----------------------------------------------------------------------------------

	def test_a_surface_is_hidden_without_its_permission(self):
		"""An armed business line grants nothing to a caller who may not read a deal in the first place."""
		self._arm()
		self._entitled_to(ARMED)
		self._as_stranger()
		self.assertFalse(frappe.has_permission("CRM Deal", "read"),
						 "fixture: the stranger can read deals, so this assertion proved nothing")
		self.assertTrue(frappe.db.get_value("CRM Vertical", ARMED, "deals_enabled"),
						"fixture: the line is not armed, so the permission half was not what refused")
		self.assertFalse(my_surfaces()["deals"], "a surface appeared for a caller who cannot read its records")

	def test_a_surface_is_hidden_when_its_toggle_is_off(self):
		"""Permission held on every line the caller works, and not one of them sells a deal."""
		self._arm()
		self._entitled_to(UNARMED)
		self.assertTrue(frappe.has_permission("CRM Deal", "read"),
						"fixture: the caller cannot read deals, so the toggle was not what refused")
		self.assertFalse(my_surfaces()["deals"], "Deals appeared for a caller with no deal-bearing line")

	def test_a_surface_shows_when_both_halves_pass(self):
		self._arm()
		self._entitled_to(ARMED, UNARMED)
		self.assertTrue(my_surfaces()["deals"], "Deals stayed hidden with permission held and a line armed")

	def test_contacts_and_organizations_ride_the_same_liveness(self):
		"""Not a second rule: the customer surfaces appear exactly when a line has been armed to sell."""
		self._arm()
		self._entitled_to(UNARMED)
		answer = my_surfaces()
		self.assertFalse(answer["contacts"], "Contacts appeared for a caller with no deal-bearing line")
		self.assertFalse(answer["organizations"],
						 "Organizations appeared for a caller with no deal-bearing line")
		self._entitled_to(ARMED)
		answer = my_surfaces()
		self.assertTrue(answer["contacts"], "Contacts stayed hidden with permission held and a line armed")
		self.assertTrue(answer["organizations"],
						"Organizations stayed hidden with permission held and a line armed")

	def test_a_customer_surface_is_hidden_without_its_doctype_permission(self):
		"""The permission half is asked of each surface's OWN doctype, so an armed line grants nothing to a
		caller who may not read the records behind it."""
		self._arm()
		self._entitled_to(ARMED)
		for key, doctype in (("contacts", "Contact"), ("organizations", "CRM Organization")):
			with self.subTest(surface=key):
				with patch("frappe.has_permission",
						   side_effect=lambda dt, *a, **kw: dt != doctype):
					self.assertFalse(my_surfaces()[key],
									 f"{key} appeared for a caller who cannot read a {doctype}")

	def test_workflows_respects_its_new_switch(self):
		"""The half Workflows never had: permission says who MAY author, this says whether the screen exists."""
		self._arm()
		self.assertTrue(frappe.has_permission("CRM Workflow", "read"),
						"fixture: the caller cannot read workflows, so the switch was not what refused")
		self.assertFalse(my_surfaces()["workflows"], "the Workflows screen appeared while its switch was off")
		self._arm(WORKFLOW_SURFACE_SWITCH)
		self.assertTrue(my_surfaces()["workflows"], "the Workflows screen stayed hidden with both halves passing")

	def test_near_me_still_answers_exactly_as_its_own_rule_does(self):
		"""Near Me owns its rule; this map only asks it. The day the two disagree, this goes red."""
		user = frappe.get_doc("User", STRANGER)
		answers = []
		for switch_on in (True, False):
			for has_role in (True, False):
				frappe.set_user("Administrator")
				if has_role:
					user.add_roles(near_me_api.ROLE)
				else:
					user.remove_roles(near_me_api.ROLE)
				self._arm(*([near_me_api.SWITCH] if switch_on else []))
				frappe.set_user(STRANGER)
				answer = my_surfaces()["near_me"]
				self.assertEqual(answer, near_me_api._can_access(),
								 f"the map and Near Me's own rule disagree (switch={switch_on}, role={has_role})")
				answers.append(answer)
		frappe.set_user("Administrator")
		self.assertIn(True, answers, "fixture: no combination was ever visible, so nothing was compared")

	def test_a_broken_surface_never_breaks_boot(self):
		"""This map rides the boot payload: a gate that raises costs a menu item, never the whole app."""
		self._arm(WORKFLOW_SURFACE_SWITCH)
		self._entitled_to(ARMED)
		with patch("tatva_connect.near_me.api._can_access", side_effect=RuntimeError("gate exploded")):
			answer = my_surfaces()
		self.assertEqual(set(answer), {"near_me", "workflows", "deals", "contacts", "organizations"},
						 "the map did not answer at all")
		self.assertFalse(answer["near_me"], "a surface that could not answer was shown anyway")
		self.assertTrue(answer["workflows"], "one broken gate took a healthy surface down with it")
		self.assertTrue(answer["deals"], "one broken gate took a healthy surface down with it")
		self.assertTrue(answer["contacts"], "one broken gate took a healthy surface down with it")
		self.assertTrue(answer["organizations"], "one broken gate took a healthy surface down with it")

	def test_the_map_costs_no_more_than_the_budget(self):
		"""Counted, not reasoned. Warmed first, because the budget is what a real boot pays after cache.

		This is also the assertion that Contacts and Organizations were added for FREE: they share the one
		liveness read Deals already pays for, so the budget did not move when they joined."""
		self._arm(WORKFLOW_SURFACE_SWITCH)
		self._entitled_to(ARMED)
		my_surfaces()
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as counter:
			my_surfaces()
		self.assertLessEqual(counter.call_count, BUDGET_QUERIES,
							 f"the surface map issued {counter.call_count} queries, over the budget of {BUDGET_QUERIES}")
