# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The grain brain's SOURCE SWAP — entitlement from User Permission, clamped by the CRM Grain registry.

LAYER 1 of two. These build their own fixture users, permissions and registry rows and assert LOGIC.
They never assert a real tuple count ("Anaya has 4 programmes"), because the test database carries no
business data and such an assertion passes by being empty. The real-data claims — flag-OFF byte-identical
per consumer, a real rep resolving their region, a Sigrima lead's Data tab resolving its full field set —
are LAYER 2, a live bench run recorded as Gate 1 evidence.

The load-bearing thing pinned here that is easy to miss: our grain User Permissions are NARROW
(`applicable_for`), so each is stored TWICE — once for CRM Lead, once for CRM Deal. Reading them with a
raw query double-counts every axis and silently multiplies the region. `test_duplicate_applicable_for_*`
is the guard, and the `ignore_user_permissions` history-field pin is beside it because those two Link
fields are now load-bearing for row visibility under the swapped source.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.access.test_grain_brain_swap
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement

V = "ZZ Swap Vertical"
G = "ZZ Swap Group"
P1 = "ZZ Swap Program One"
P2 = "ZZ Swap Program Two"
USER = "zz-swap-rep@example.test"

_FLAG = "tatva_connect.access.entitlement._registry_enabled"


class TestGrainBrainSwap(FrappeTestCase):
	def setUp(self):
		self._master("CRM Vertical", "vertical_name", V)
		self._master("CRM Group", "group_name", G)
		self._master("CRM Program", "program_name", P1)
		self._master("CRM Program", "program_name", P2)
		if not frappe.db.exists("User", USER):
			user = frappe.new_doc("User")
			user.email, user.first_name, user.send_welcome_email = USER, "ZZ Swap", 0
			user.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		frappe.db.commit()
		self._clear_request_cache()

	def tearDown(self):
		frappe.db.delete("User Permission", {"user": USER})
		frappe.db.sql("DELETE FROM `tabCRM Grain` WHERE vertical = %s", (V,))
		frappe.db.delete("User", {"name": USER})
		for doctype, name in (
			("CRM Program", P1), ("CRM Program", P2), ("CRM Group", G), ("CRM Vertical", V),
		):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()
		self._clear_request_cache()

	# ---- fixtures -------------------------------------------------------------------------

	def _master(self, doctype, fieldname, value):
		if frappe.db.exists(doctype, value):
			return
		doc = frappe.new_doc(doctype)
		doc.set(fieldname, value)
		doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user

	def _clear_request_cache(self):
		"""entitled_grains and the flag are request-cached; a test that flips either must start clean."""
		for bucket in (
			entitlement._GRAINS_CACHE, entitlement._REGISTRY_FLAG_CACHE, entitlement._REGISTRY_ROWS_CACHE,
		):
			if hasattr(frappe.local, bucket):
				delattr(frappe.local, bucket)

	def _permit(self, allow, value, applicable_for="CRM Lead"):
		up = frappe.new_doc("User Permission")
		up.user, up.allow, up.for_value = USER, allow, value
		up.applicable_for = applicable_for
		up.apply_to_all_doctypes = 0
		up.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		self._clear_request_cache()

	def _grain_row(self, vertical, group, program):
		doc = frappe.new_doc("CRM Grain")
		doc.vertical, doc.group, doc.program = vertical, group, program or None
		doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		self._clear_request_cache()

	# ---- the User Permission source -------------------------------------------------------

	def test_no_permission_is_empty_not_a_wildcard(self):
		"""Fail-closed. A blank ('','','') tuple would be a three-way wildcard granting everything."""
		self.assertEqual(entitlement._grains_from_user_permission(USER), set())

	def test_all_three_axes_give_one_concrete_grain(self):
		self._permit("CRM Vertical", V)
		self._permit("CRM Group", G)
		self._permit("CRM Program", P1)
		self.assertEqual(entitlement._grains_from_user_permission(USER), {(V, G, P1)})

	def test_missing_program_axis_stays_blank_as_a_wildcard(self):
		"""The wildcard rep — entitled to a whole group, no programme permission. The blank axis is what
		lets them work every programme under it, including one with no leads yet."""
		self._permit("CRM Vertical", V)
		self._permit("CRM Group", G)
		self.assertEqual(entitlement._grains_from_user_permission(USER), {(V, G, "")})

	def test_two_programs_give_the_cross_product(self):
		"""Frappe AND-s permissions across doctypes, so two allowed programmes are two regions."""
		self._permit("CRM Vertical", V)
		self._permit("CRM Group", G)
		self._permit("CRM Program", P1)
		self._permit("CRM Program", P2)
		self.assertEqual(entitlement._grains_from_user_permission(USER), {(V, G, P1), (V, G, P2)})

	def test_a_crm_deal_permission_does_not_widen_the_lead_region(self):
		"""THE trap, and it only bites when the two differ. Our grain permissions are stored once for
		CRM Lead and once for CRM Deal; a raw User Permission query returns BOTH rows. Where the values
		match, the duplicate collapses harmlessly — so a test using the same value on both proves nothing
		(an earlier draft of this test did exactly that and stayed green against a raw query). Give the
		CRM Deal row a DIFFERENT programme and the bug is visible: the region silently doubles to include
		a programme the user may not touch on a lead."""
		self._permit("CRM Vertical", V)
		self._permit("CRM Group", G)
		self._permit("CRM Program", P1)
		self._permit("CRM Program", P2, applicable_for="CRM Deal")
		self.assertEqual(entitlement._grains_from_user_permission(USER), {(V, G, P1)})

	def test_a_crm_deal_only_permission_grants_no_lead_region(self):
		self._permit("CRM Vertical", V, applicable_for="CRM Deal")
		self._permit("CRM Group", G, applicable_for="CRM Deal")
		self.assertEqual(entitlement._grains_from_user_permission(USER), set())

	# ---- the swap itself ------------------------------------------------------------------

	def test_flag_off_uses_the_assignment_rule_source(self):
		"""Flag OFF must not consult User Permission at all — that is what makes a disarm a true revert."""
		self._permit("CRM Vertical", V)
		self._permit("CRM Group", G)
		with patch(_FLAG, return_value=False), \
		     patch.object(entitlement, "_internal_grains", return_value={("SENTINEL", "", "")}) as ar:
			self.assertEqual(entitlement.entitled_grains(USER), {("SENTINEL", "", "")})
			ar.assert_called_once()

	def test_flag_on_uses_the_user_permission_source(self):
		self._permit("CRM Vertical", V)
		self._permit("CRM Group", G)
		with patch(_FLAG, return_value=True), \
		     patch.object(entitlement, "_internal_grains", return_value={("SENTINEL", "", "")}) as ar:
			self.assertEqual(entitlement.entitled_grains(USER), {(V, G, "")})
			ar.assert_not_called()

	# ---- the registry clamp on grain_entitled ---------------------------------------------

	def test_flag_on_rejects_a_grain_absent_from_the_registry(self):
		"""Structural enforcement: the region COVERS (V, G, P2), but P2 is not a declared combination."""
		self._permit("CRM Vertical", V)
		self._permit("CRM Group", G)
		self._grain_row(V, G, P1)
		with patch(_FLAG, return_value=True):
			self.assertTrue(entitlement.grain_entitled((V, G, P1), user=USER))
			self.assertFalse(entitlement.grain_entitled((V, G, P2), user=USER))

	def test_flag_off_ignores_the_registry(self):
		"""Same un-declared grain, flag OFF -> the covers check alone decides, exactly as today."""
		with patch(_FLAG, return_value=False), \
		     patch.object(entitlement, "_internal_grains", return_value={(V, G, "")}):
			self.assertTrue(entitlement.grain_entitled((V, G, P2), user=USER))

	def test_registry_clamp_is_alongside_covers_not_instead_of_it(self):
		"""A declared grain OUTSIDE the user's region is still refused — the clamp adds, never replaces."""
		self._permit("CRM Vertical", V)
		self._permit("CRM Group", G)
		self._permit("CRM Program", P1)
		self._grain_row(V, G, P2)
		with patch(_FLAG, return_value=True):
			self.assertFalse(entitlement.grain_entitled((V, G, P2), user=USER))

	def test_system_manager_keeps_the_bypass(self):
		"""Deliberate: the clamp must not lock an admin out of the off-registry rows they exist to fix."""
		with patch(_FLAG, return_value=True), \
		     patch.object(entitlement, "entitled_grains", return_value=entitlement.ALL_GRAINS):
			self.assertTrue(entitlement.grain_entitled((V, G, "ZZ Undeclared"), user=USER))

	# ---- the ignore_user_permissions pin --------------------------------------------------

	def test_history_link_fields_stay_exempt_from_user_permissions(self):
		"""LOAD-BEARING under the swap. custom_previous_program / custom_origin_vertical are Links to grain
		masters that record where a lead HAS BEEN. Entitlement is now the user's User Permission set, and
		frappe applies those to EVERY non-exempt Link on the doctype — so if either setter is flipped to 0,
		a lead whose history names a programme outside the viewer's permission disappears from their list
		entirely. The lead's CURRENT axes must be enforced; its history must not be."""
		meta = frappe.get_meta("CRM Lead")
		for fieldname in ("custom_previous_program", "custom_origin_vertical"):
			field = meta.get_field(fieldname)
			self.assertIsNotNone(field, f"{fieldname} must exist — entitlement depends on its exemption")
			self.assertEqual(
				field.ignore_user_permissions, 1,
				f"{fieldname}.ignore_user_permissions must stay 1: it is a history field, and enforcing "
				"User Permissions on it hides leads whose PAST grain is outside the viewer's region.",
			)

	def test_current_grain_link_fields_are_NOT_exempt(self):
		"""The other half of the same rule — the axes that decide a lead's real grain must stay enforced,
		or row scoping silently stops applying."""
		meta = frappe.get_meta("CRM Lead")
		for fieldname in ("custom_vertical", "custom_group", "custom_current_program"):
			field = meta.get_field(fieldname)
			self.assertEqual(
				field.ignore_user_permissions, 0,
				f"{fieldname}.ignore_user_permissions must stay 0 — it carries the lead's real grain.",
			)

	# ---- the inert registry readers -------------------------------------------------------

	def test_programs_under_returns_declared_children_without_the_blank(self):
		"""A blank-programme row is a REGION, not a pickable child."""
		self._grain_row(V, G, "")
		self._grain_row(V, G, P1)
		self._grain_row(V, G, P2)
		self.assertEqual(entitlement.programs_under(V, G), [P1, P2])

	def test_groups_under_returns_declared_groups(self):
		self._grain_row(V, G, P1)
		self.assertEqual(entitlement.groups_under(V), [G])
