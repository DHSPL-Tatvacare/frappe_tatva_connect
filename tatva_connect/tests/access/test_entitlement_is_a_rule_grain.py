# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An entitlement is a rule, so its blank axis means any; a lead's own grain is data and matches literally.
Own vertical, groups, contracts and real stock catalog columns; no DDL, no commit."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import entitlement
from tatva_connect.tests.api import partner_fixture

VERTICAL = "ZZ Rule Grain Vertical"
GROUP_ONE = "ZZ Rule Grain Group One"
GROUP_TWO = "ZZ Rule Grain Group Two"


class TestEntitlementIsARuleGrain(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("CRM Vertical", VERTICAL):
			frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": VERTICAL}).insert(ignore_permissions=True)
		for group in (GROUP_ONE, GROUP_TWO):
			if not frappe.db.exists("CRM Group", group):
				frappe.get_doc({"doctype": "CRM Group", "group_name": group}).insert(ignore_permissions=True)

		# One catalog row per group, ticked only by that group's contract, so a wrong matcher shows.
		cls.field_one, cls.field_two = partner_fixture.stock_catalog_rows(2)
		cls.contracts = [
			cls._contract(GROUP_ONE, cls.field_one),
			cls._contract(GROUP_TWO, cls.field_two),
		]
		cls._forget()

	@classmethod
	def _contract(cls, group, field_key):
		doc = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": f"ZZ Rule Grain {group}", "enabled": 1,
			"is_internal": 1, "vertical": VERTICAL, "crm_group": group,
			"allowed_fields": [{"field": field_key}],
		}).insert(ignore_permissions=True)
		return doc.name

	@staticmethod
	def _forget():
		"""The ticks are request-cached; a test that mints a contract inside one must drop what it read."""
		for bucket in ("tatva_connect:internal_contract_ticks", "tatva_connect:internal_universal_fields",
		               "tatva_connect:entitled_grains"):
			setattr(frappe.local, bucket, None)

	def setUp(self):
		frappe.set_user("Administrator")
		self._forget()

	def _resolved(self, grains):
		rows = {k: {"field_key": k} for k in (self.field_one, self.field_two)}
		return set(entitlement.resolve_fields(rows, grains))

	# -- the entitlement side: a blank axis means ANY --------------------------

	def test_a_group_scoped_entitlement_reaches_only_that_group(self):
		"""The baseline. A fully-named entitlement has always worked, and must keep working."""
		self.assertEqual(self._resolved({(VERTICAL, GROUP_ONE, "")}), {self.field_one})
		self.assertEqual(self._resolved({(VERTICAL, GROUP_TWO, "")}), {self.field_two})

	def test_a_vertical_wide_entitlement_reaches_every_group_inside_it(self):
		"""An admin entitled to the whole vertical sees what every group inside it declares.
		Not only the contracts that happen to leave the group blank too."""
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
		"""A lead's grain is data, so its blank program is a real blank, not "any program".
		Widening this would show a lead fields its own grain never declared."""
		self.assertTrue(
			entitlement.field_in_grains_via_contract(self.field_one, [(VERTICAL, GROUP_ONE, "")]),
		)
		self.assertFalse(
			entitlement.field_in_grains_via_contract(self.field_one, [(VERTICAL, GROUP_TWO, "")]),
			"a lead in another group must never pick up this group's field",
		)

	def test_a_contract_with_no_programme_covers_a_lead_in_any_programme(self):
		"""A lead enrolled in any programme still gets its vertical+group contract's fields."""
		self.assertTrue(entitlement.field_in_grains_via_contract(self.field_one, [(VERTICAL, GROUP_ONE, "Some Enrolled Programme")]))
		self.assertFalse(entitlement.field_in_grains_via_contract(self.field_one, [("ZZ Not A Vertical", GROUP_ONE, "")]))
