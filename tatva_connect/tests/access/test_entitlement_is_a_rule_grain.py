# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An entitlement is a RULE, so a blank axis on it means ANY — not the empty string.

`taxonomy/grain.py` declares the distinction and names the trap in its own docstring: `covers` asks about
a real RECORD and only the candidate may wildcard, `overlaps` asks about two RULES and either side may.
`resolve_fields` was handed `entitled_grains()` — rules — and asked the record question about them, so an
entitlement carrying a blank axis matched only contracts blank in the same place.

Measured before the fix, on live contracts: a viewer entitled to `('Goodflip-Care','Anaya','')` resolved
131 fields; one entitled to `('Goodflip-Care','','')` — the same vertical, no group named, which is what a
cross-functional admin holds — resolved **2**. The admin saw less than the rep beneath them, and their
Smart View filter and column pickers came up empty.

This mints its own contracts and catalog rows so it asserts the CODE, not a site's seed.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_entitlement_is_a_rule_grain
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement
from tatva_connect.tests.api import partner_fixture

VERTICAL = "ZZ Rule Grain Vertical"
GROUP_ONE = "ZZ Rule Grain Group One"
GROUP_TWO = "ZZ Rule Grain Group Two"


class TestEntitlementIsARuleGrain(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("CRM Vertical", VERTICAL):
			frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": VERTICAL}).insert(ignore_permissions=True)
		for group in (GROUP_ONE, GROUP_TWO):
			if not frappe.db.exists("CRM Group", group):
				frappe.get_doc({"doctype": "CRM Group", "group_name": group}).insert(ignore_permissions=True)

		# One catalog row per group, each ticked by only that group's contract — so "which fields does
		# this entitlement reach" has a different right answer per grain, and a wrong matcher shows it.
		cls.field_one = partner_fixture.mint_catalog_row("zz_rule_grain_one")
		cls.field_two = partner_fixture.mint_catalog_row("zz_rule_grain_two")
		cls.contracts = [
			cls._contract(GROUP_ONE, cls.field_one),
			cls._contract(GROUP_TWO, cls.field_two),
		]
		cls._forget()
		frappe.db.commit()

	@classmethod
	def _contract(cls, group, field_key):
		doc = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": f"ZZ Rule Grain {group}", "enabled": 1,
			"is_internal": 1, "vertical": VERTICAL, "crm_group": group,
			"allowed_fields": [{"field": field_key}],
		}).insert(ignore_permissions=True)
		return doc.name

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in cls.contracts:
			if frappe.db.exists("CRM Lead API Mapping", name):
				frappe.delete_doc("CRM Lead API Mapping", name, force=True, ignore_permissions=True)
		partner_fixture.teardown()
		for dt, name in (("CRM Group", GROUP_ONE), ("CRM Group", GROUP_TWO), ("CRM Vertical", VERTICAL)):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		cls._forget()
		frappe.db.commit()
		super().tearDownClass()

	@staticmethod
	def _forget():
		"""The ticks are request-cached; a test that mints a contract inside one must drop what it read."""
		for bucket in ("tatva_connect:internal_contract_ticks", "tatva_connect:internal_universal_fields",
		               "tatva_connect:field_restrictions", "tatva_connect:entitled_grains"):
			setattr(frappe.local, bucket, None)

	def setUp(self):
		frappe.set_user("Administrator")
		self._forget()

	def _resolved(self, grains):
		rows = {k: {"field_key": k} for k in (self.field_one, self.field_two)}
		return set(entitlement.resolve_fields(rows, grains, []))

	# -- the entitlement side: a blank axis means ANY --------------------------

	def test_a_group_scoped_entitlement_reaches_only_that_group(self):
		"""The baseline. A fully-named entitlement has always worked, and must keep working."""
		self.assertEqual(self._resolved({(VERTICAL, GROUP_ONE, "")}), {self.field_one})
		self.assertEqual(self._resolved({(VERTICAL, GROUP_TWO, "")}), {self.field_two})

	def test_a_vertical_wide_entitlement_reaches_every_group_inside_it(self):
		"""THE defect. A cross-functional admin entitled to the whole vertical must see what every group
		inside it declares — not the empty intersection of contracts that happen to be equally blank."""
		self.assertEqual(
			self._resolved({(VERTICAL, "", "")}), {self.field_one, self.field_two},
			"a vertical-wide entitlement must reach every group's fields",
		)

	def test_two_named_grains_still_union(self):
		"""An admin holding two specific grains sees both — this path already worked and must not move."""
		self.assertEqual(
			self._resolved({(VERTICAL, GROUP_ONE, ""), (VERTICAL, GROUP_TWO, "")}),
			{self.field_one, self.field_two},
		)

	# -- the lead side is a DATA grain and must NOT wildcard -------------------

	def test_a_leads_own_grain_still_matches_literally(self):
		"""The other half of the distinction. A LEAD's grain is data: its blank program is a real blank,
		not "any program". Widening this would show a lead fields its own grain never declared."""
		self.assertTrue(
			entitlement.field_in_grains_via_contract(self.field_one, [(VERTICAL, GROUP_ONE, "")]),
		)
		self.assertFalse(
			entitlement.field_in_grains_via_contract(self.field_one, [(VERTICAL, GROUP_TWO, "")]),
			"a lead in another group must never pick up this group's field",
		)
