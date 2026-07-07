# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 2 - the typed, pick-aware field catalog (`describe.field_catalog`) that the v2 predicate
builder renders real controls from (Link search / Select options / child path) instead of free
text. Every assertion reads the pick source off the LIVE CRM Lead meta - never a hardcoded
doctype/option list - so the test can never drift from the schema it's describing (S.6).
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import describe

_DOCTYPE = "CRM Lead"


class TestDescribeFieldCatalog(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")

	def _catalog(self):
		return describe.field_catalog(_DOCTYPE)

	# A Link field carries pick.kind=="link" with the target read off the live meta.
	def test_link_field_carries_target(self):
		catalog = self._catalog()
		entry = next(e for e in catalog if e["key"] == "custom_substage")
		live_target = frappe.get_meta(_DOCTYPE).get_field("custom_substage").options
		self.assertEqual(entry["pick"], {"kind": "link", "target": live_target})

	# A Select field carries pick.kind=="select" with its real options (split, blanks dropped).
	def test_select_field_carries_options(self):
		catalog = self._catalog()
		entry = next(e for e in catalog if e["key"] == "custom_lead_temperature")
		live_options = frappe.get_meta(_DOCTYPE).get_field("custom_lead_temperature").options
		expected = [o.strip() for o in live_options.split("\n") if o.strip()]
		self.assertEqual(entry["pick"], {"kind": "select", "options": expected})

	# A child-table field appears once, one level deep, under a dotted key and pick.kind=="child".
	def test_child_table_field_gets_dotted_path(self):
		catalog = self._catalog()
		entry = next(e for e in catalog if e["key"] == "products.product_name")
		self.assertEqual(entry["pick"], {"kind": "child", "path": "products.product_name"})
		live_type = frappe.get_meta("CRM Products").get_field("product_name").fieldtype
		self.assertEqual(entry["type"], live_type)

	# Planted-bad: a plain Data field (not Link/Select/child) MUST carry pick=None, not a false pick.
	def test_plain_data_field_has_no_pick(self):
		catalog = self._catalog()
		entry = next(e for e in catalog if e["key"] == "first_name")
		self.assertIsNone(entry["pick"])

	# Planted-bad: a field absent from the meta must not appear in the catalog.
	def test_unknown_field_is_absent(self):
		catalog = self._catalog()
		keys = [e["key"] for e in catalog]
		self.assertNotIn("this_field_does_not_exist", keys)

	# No doctype -> empty catalog, never a false offering.
	def test_no_doctype_is_empty(self):
		self.assertEqual(describe.field_catalog(None), [])


if __name__ == "__main__":
	unittest.main()
