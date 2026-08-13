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

from tatva_connect.automation import describe, fields
from tatva_connect.taxonomy import picklist

_DOCTYPE = "CRM Lead"
_PICKLIST_DT = "CRM Picklist Value"
_QUERY = "tatva_connect.taxonomy.picklist.picklist_query"


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


def _picklist_links():
	"""Every Link at CRM Picklist Value on the live subject meta — read, never listed here."""
	return [df for df in frappe.get_meta(_DOCTYPE).fields if df.fieldtype == "Link" and df.options == _PICKLIST_DT]


def _other_link():
	"""A Link at any OTHER master — the control case a picklist-only change must leave untouched."""
	return next(
		df for df in frappe.get_meta(_DOCTYPE).fields
		if df.fieldtype == "Link" and df.options and df.options != _PICKLIST_DT
	)


class TestAPicklistLinkNamesTheResolverThatAlreadyExists(FrappeTestCase):
	"""The descriptor carried the target doctype and nothing else, so the editor searched the WHOLE master:
	an Anaya workflow's Zone picker offered another category and another business line. `picklist_query` has
	always been the resolver — category-scoped, grain-scoped, entitlement-clamped — and is what every lead
	form uses. This asserts the descriptor names it rather than the editor re-deriving its rule."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def test_the_subject_really_has_one_or_this_asserts_nothing(self):
		self.assertTrue(_picklist_links(), f"{_DOCTYPE} carries no Link at {_PICKLIST_DT}")

	def test_it_names_the_query_and_its_own_category(self):
		catalog = {entry["key"]: entry for entry in describe.field_catalog(_DOCTYPE)}
		for df in _picklist_links():
			with self.subTest(field=df.fieldname):
				pick = catalog[df.fieldname]["pick"]
				self.assertEqual(pick["kind"], "link")
				self.assertEqual(pick["target"], _PICKLIST_DT)
				self.assertEqual(pick["query"], _QUERY)
				self.assertEqual(pick["filters"]["category"], picklist.category_of(df.fieldname))

	def test_a_link_at_any_other_master_is_unchanged(self):
		df = _other_link()
		catalog = {entry["key"]: entry for entry in describe.field_catalog(_DOCTYPE)}
		self.assertEqual(catalog[df.fieldname]["pick"], {"kind": "link", "target": df.options})


class TestTheGrainRidesInTheSameFilters(FrappeTestCase):
	"""The grain is added by the caller that knows it — `builder_schema` already takes the three axes off
	the Trigger. The rule itself is never restated: `picklist_query` owns it."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	@staticmethod
	def _grain_that_entitles_a_picklist_field():
		"""A declared grain whose contract really reads one — read off the live rows, never a seeded name
		typed here, which would drift the day the seed moves."""
		links = {df.fieldname for df in _picklist_links()}
		for grain in frappe.get_all("CRM Grain", fields=["vertical", "group", "program"]):
			axes = (grain.vertical or "", grain.group or "", grain.program or "")
			if {r.fieldname for r in fields.readable_rows_in_rule_grain(_DOCTYPE, axes)} & links:
				return axes
		return None

	def test_every_picklist_filter_carries_the_axes_it_was_asked_for(self):
		axes = self._grain_that_entitles_a_picklist_field()
		self.assertIsNotNone(axes, f"no declared grain reads a {_PICKLIST_DT} field — nothing would be asserted")
		vertical, group, program = axes
		schema = describe.builder_schema(
			on_doctype=_DOCTYPE, vertical=vertical, group=group, program=program
		)
		scoped = 0
		for entry in [*schema["fields"], *schema["set_targets"]]:
			filters = (entry.get("pick") or {}).get("filters")
			if filters is None:
				continue
			scoped += 1
			with self.subTest(field=entry["key"]):
				self.assertEqual(filters["vertical"], vertical)
				self.assertEqual(filters["group"], group)
				self.assertEqual(filters["program"], program)
		self.assertTrue(scoped, "no descriptor carried a picklist filter, so the grain was never checked")


if __name__ == "__main__":
	unittest.main()
