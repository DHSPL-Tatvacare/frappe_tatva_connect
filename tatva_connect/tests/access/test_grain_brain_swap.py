# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Entitlement read from native User Permission, clamped by the CRM Grain registry, behind the real flag.
Own masters, user, permissions and registry rows; each test rolls back to its own savepoint."""

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import entitlement

V = "ZZ Swap Vertical"
G = "ZZ Swap Group"
P1 = "ZZ Swap Program One"
P2 = "ZZ Swap Program Two"
USER = "grainswap.rep@example.test"
FLAG = "Access::Grain::registry"


class TestGrainBrainSwap(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for doctype, fieldname, value in (("CRM Vertical", "vertical_name", V), ("CRM Group", "group_name", G),
		                                  ("CRM Program", "program_name", P1), ("CRM Program", "program_name", P2)):
			if not frappe.db.exists(doctype, value):
				frappe.get_doc({"doctype": doctype, fieldname: value}).insert(ignore_permissions=True)
		if not frappe.db.exists("User", USER):
			frappe.get_doc({"doctype": "User", "email": USER, "first_name": "Grain Swap", "send_welcome_email": 0,
			                "roles": [{"role": "Sales User"}]}).insert(ignore_permissions=True)

	def setUp(self):
		frappe.db.savepoint("grain_swap")
		self._forget()

	def tearDown(self):
		frappe.db.rollback(save_point="grain_swap")
		frappe.clear_document_cache("CRM Tatva Automation", FLAG)
		frappe.cache.hdel("user_permissions", USER)  # a rollback fires no hook, so drop the cached copy
		self._forget()

	@staticmethod
	def _forget():
		for bucket in (entitlement._GRAINS_CACHE, entitlement._REGISTRY_FLAG_CACHE, entitlement._REGISTRY_ROWS_CACHE):
			if hasattr(frappe.local, bucket):
				delattr(frappe.local, bucket)

	def _flag(self, enabled):
		row = frappe.get_doc("CRM Tatva Automation", FLAG)
		row.enabled = enabled
		row.save(ignore_permissions=True)
		self._forget()

	def _permit(self, allow, value, applicable_for="CRM Lead"):
		frappe.get_doc({"doctype": "User Permission", "user": USER, "allow": allow, "for_value": value,
		                "applicable_for": applicable_for, "apply_to_all_doctypes": 0}).insert(ignore_permissions=True)
		self._forget()

	def _declare(self, program):
		frappe.get_doc({"doctype": "CRM Grain", "vertical": V, "group": G, "program": program or None}).insert(
			ignore_permissions=True)
		self._forget()

	def test_no_permission_is_an_empty_region_not_a_wildcard(self):
		self.assertEqual(entitlement._grains_from_user_permission(USER), set())

	def test_permissions_on_each_axis_make_the_region(self):
		self._permit("CRM Vertical", V)
		self._permit("CRM Group", G)
		self.assertEqual(entitlement._grains_from_user_permission(USER), {(V, G, "")}, "no program permission = every program")
		self._permit("CRM Program", P1)
		self._permit("CRM Program", P2)
		self.assertEqual(entitlement._grains_from_user_permission(USER), {(V, G, P1), (V, G, P2)})

	def test_a_crm_deal_permission_never_widens_the_lead_region(self):
		"""Grain permissions are stored per applicable_for; the Deal copy must never add a lead programme."""
		self._permit("CRM Vertical", V)
		self._permit("CRM Group", G)
		self._permit("CRM Program", P1)
		self._permit("CRM Program", P2, applicable_for="CRM Deal")
		self.assertEqual(entitlement._grains_from_user_permission(USER), {(V, G, P1)})
		for name in frappe.get_all("User Permission", filters={"user": USER, "applicable_for": "CRM Lead"}, pluck="name"):
			frappe.delete_doc("User Permission", name, ignore_permissions=True)
		self._forget()
		self.assertEqual(entitlement._grains_from_user_permission(USER), set(), "Deal-only permissions grant no lead region")

	def test_the_flag_chooses_the_source(self):
		self._permit("CRM Vertical", V)
		self._permit("CRM Group", G)
		self._flag(1)
		self.assertEqual(entitlement.entitled_grains(USER), {(V, G, "")})
		self._flag(0)
		self.assertEqual(entitlement.entitled_grains(USER), set(), "off, User Permission must not be read at all")

	def test_the_registry_clamps_the_region_when_armed(self):
		self._permit("CRM Vertical", V)
		self._permit("CRM Group", G)
		self._declare(P1)
		self._flag(1)
		self.assertTrue(entitlement.grain_entitled((V, G, P1), user=USER))
		self.assertFalse(entitlement.grain_entitled((V, G, P2), user=USER), "covered but undeclared must be refused")
		self.assertTrue(entitlement.grain_entitled((V, G, "ZZ Undeclared"), user="Administrator"), "System Manager keeps the bypass")

	def test_a_declared_grain_outside_the_region_is_refused(self):
		self._permit("CRM Vertical", V)
		self._permit("CRM Group", G)
		self._permit("CRM Program", P1)
		self._declare(P2)
		self._flag(1)
		self.assertFalse(entitlement.grain_entitled((V, G, P2), user=USER))

	def test_history_fields_stay_exempt_and_current_axes_stay_enforced(self):
		"""Flip either and a lead vanishes for its history, or row scoping silently stops applying."""
		meta = frappe.get_meta("CRM Lead")
		for fieldname, exempt in (("custom_previous_program", 1), ("custom_origin_vertical", 1), ("custom_vertical", 0),
		                          ("custom_group", 0), ("custom_current_program", 0)):
			with self.subTest(field=fieldname):
				self.assertEqual(meta.get_field(fieldname).ignore_user_permissions, exempt)
