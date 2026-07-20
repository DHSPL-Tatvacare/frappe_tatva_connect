# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The field gate, asked by a REAL non-System-Manager user — the lock nothing else was holding.

`resolve_fields` is the ONE brain for "which fields may an internal user see". Before this test, it could
have returned the WHOLE catalog and the suite would have stayed green: every test that reaches it end to
end runs as **Administrator**, whose `entitled_grains` short-circuits to `ALL_GRAINS`, and `ALL_GRAINS`
returns True from `entitled_to_field` before the catalog is ever consulted. The gate was never asked a
question it could get wrong. That is exactly the shape of B3 — a declaration nothing checks.

The tests that do exercise the gate hand `resolve_fields` a grain tuple by hand, so they lock the MATCHER
but not the WIRING: `entitled_grains` could return the wrong thing, or the caller could stop consulting it,
and only this test would notice. So this one names no grain. It mints a user, gives them entitlement the
way production does — membership in a live `CRM Lead Assignment Rule` — becomes that user, and lets the
brain resolve its own grains from the session.

Everything is minted: two verticals' worth of masters, two internal contracts, two catalog rows. Nothing
is read off a site's seed, so a reseed can never turn this red for a reason that is no defect.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_resolve_fields_gates_a_real_user
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement
from tatva_connect.tests.api import partner_fixture

VERTICAL = "ZZ Real User Vertical"
GROUP_HELD = "ZZ Real User Group Held"
GROUP_FOREIGN = "ZZ Real User Group Foreign"

USER = "zz-real-user-gate@example.invalid"
RULE = "ZZ Real User Gate Rule"

_MAPPING = "CRM Lead API Mapping"
_CACHES = (
	"tatva_connect:entitled_grains",
	"tatva_connect:internal_contract_ticks",
	"tatva_connect:internal_universal_fields",
	"tatva_connect:field_restrictions",
)


class TestResolveFieldsGatesARealUser(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("CRM Vertical", VERTICAL):
			frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": VERTICAL}).insert(ignore_permissions=True)
		for group in (GROUP_HELD, GROUP_FOREIGN):
			if not frappe.db.exists("CRM Group", group):
				frappe.get_doc({"doctype": "CRM Group", "group_name": group}).insert(ignore_permissions=True)

		# One catalog row per group, each ticked by only that group's contract: the held field and the
		# foreign one are otherwise identical, so only the grain gate can tell them apart.
		cls.field_held = partner_fixture.mint_catalog_row("zz_real_user_held")
		cls.field_foreign = partner_fixture.mint_catalog_row("zz_real_user_foreign")
		cls.contracts = [
			cls._contract(GROUP_HELD, cls.field_held),
			cls._contract(GROUP_FOREIGN, cls.field_foreign),
		]
		cls._user()
		cls._rule()
		cls._forget()
		frappe.db.commit()

	@classmethod
	def _contract(cls, group, field_key):
		doc = frappe.get_doc({
			"doctype": _MAPPING, "contract_name": f"ZZ Real User Gate {group}", "enabled": 1,
			"is_internal": 1, "vertical": VERTICAL, "crm_group": group,
			"allowed_fields": [{"field": field_key}],
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		return doc.name

	@classmethod
	def _user(cls):
		"""A plain System User with NO System Manager role — the whole point. A System Manager resolves
		ALL_GRAINS and never reaches the catalog, which is why every prior test was blind here."""
		if not frappe.db.exists("User", USER):
			frappe.get_doc({
				"doctype": "User", "email": USER, "first_name": "ZZ Real User", "enabled": 1,
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	@classmethod
	def _rule(cls):
		"""A live Assignment Rule at the HELD grain — the source `entitlement._internal_grains` reads.
		Not a stub: an internal principal's grains come from real rule rows, so a test that passed a grain
		tuple in by hand would prove nothing about the wiring this one exists to lock."""
		if not frappe.db.exists("Assignment Rule", RULE):
			frappe.get_doc({
				"doctype": "Assignment Rule", "name": RULE, "document_type": "CRM Lead",
				"description": "real-user field gate probe", "assign_condition": "1",
				"rule": "Round Robin", "priority": 0, "disabled": 0,
				"grain_vertical": VERTICAL, "grain_group": GROUP_HELD, "grain_program": "",
				"users": [{"user": USER}],
				"assignment_days": [{"day": "Monday"}],
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	@classmethod
	def tearDownClass(cls):
		"""Nothing this suite armed may outlive it — a rule, a contract or a user left behind is a live
		config change, and this one would silently widen another suite's entitlement."""
		frappe.set_user("Administrator")
		for doctype, name in (("Assignment Rule", RULE), ("User", USER)):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		for name in cls.contracts:
			if frappe.db.exists(_MAPPING, name):
				frappe.delete_doc(_MAPPING, name, force=True, ignore_permissions=True)
		partner_fixture.teardown()
		for doctype, name in (("CRM Group", GROUP_HELD), ("CRM Group", GROUP_FOREIGN),
		                      ("CRM Vertical", VERTICAL)):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		cls._forget()
		frappe.db.commit()
		super().tearDownClass()

	@staticmethod
	def _forget():
		"""Entitlement and the ticks are request-cached; a test that mints either must drop what it read."""
		for bucket in _CACHES:
			setattr(frappe.local, bucket, None)

	def setUp(self):
		frappe.set_user("Administrator")
		self._forget()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._forget()

	def _catalog(self):
		"""The scope-filtered catalog `resolve_fields` is handed — both rows, indistinguishable but for grain."""
		return {k: {"field_key": k} for k in (self.field_held, self.field_foreign)}

	def _resolved_as_the_session_user(self):
		"""The production shape: no grain is named here — the brain resolves it off the session."""
		return set(entitlement.resolve_fields(
			self._catalog(), entitlement.entitled_grains(), frappe.get_roles(),
		))

	# -- the guards, so the lock can never pass vacuously ----------------------

	def test_the_probe_is_not_a_system_manager(self):
		"""If this ever fails the lock below is meaningless — ALL_GRAINS answers True before the catalog
		is read, which is precisely how a wide-open `resolve_fields` stayed green until now."""
		self.assertNotIn("System Manager", frappe.get_roles(USER))
		self.assertNotEqual(entitlement.entitled_grains(USER), entitlement.ALL_GRAINS)

	def test_the_probe_holds_exactly_the_one_grain_its_rule_declares(self):
		"""The wiring itself: entitlement must come off the live Assignment Rule, not from nowhere."""
		self.assertEqual(entitlement.entitled_grains(USER), {(VERTICAL, GROUP_HELD, "")})

	# -- THE lock -------------------------------------------------------------

	def test_a_real_user_sees_their_own_grains_field(self):
		"""Non-vacuity. Without this, an entitlement that collapsed to nothing would pass the test below
		while breaking every Smart View the user owns."""
		frappe.set_user(USER)
		self.assertIn(
			self.field_held, self._resolved_as_the_session_user(),
			"the user's own grain declares this field — resolving it away empties their Data tab",
		)

	def test_a_real_user_never_sees_a_field_of_a_grain_they_do_not_hold(self):
		"""THE assertion. A field ticked only by the foreign group's contract must be absent for a user
		entitled to the held group — a real session, a real rule, no grain named by the test."""
		frappe.set_user(USER)
		self.assertNotIn(
			self.field_foreign, self._resolved_as_the_session_user(),
			"a field belonging to a grain this user does not hold leaked through resolve_fields",
		)

	def test_administrator_is_blind_to_this_defect(self):
		"""Why the hole existed, asserted rather than described: as Administrator the SAME call returns
		BOTH fields, correctly — so no Administrator-run test can ever fail on a broken field gate."""
		self.assertEqual(
			self._resolved_as_the_session_user(), {self.field_held, self.field_foreign},
		)
