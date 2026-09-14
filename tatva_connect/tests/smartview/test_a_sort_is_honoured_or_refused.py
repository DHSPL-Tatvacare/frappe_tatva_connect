# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A sort is honoured or refused, never ignored — and it never changes what the page COUNTS.

Two defects in the one block of `get_data` that resolves the sort key.

  * THE COUNT LOST A TERM. The count deliberately drops the join a SORT-only column needs, because a
    count has no ORDER BY. It dropped the KEY, so a predicate leaf naming the sorted column could no
    longer resolve and fell to `_never_matches()`: a view filtered on a field and sorted by that same
    field answered `total: 0` while the grid drew its rows, and a two-condition view sorted by the first
    of them raised TypeError and returned a 500.
  * THE SORT WAS SILENTLY DROPPED. The sorted column was never added to the key set the row query
    resolves terms from, so unless the view happened to PROJECT or FILTER it, the page fell back to
    `ORDER BY modified DESC` while the toolbar showed the sort as applied. The toolbar offers every
    sortable catalog field (`SmartViewList.vue`), not just the view's columns, so this was the ordinary
    case and not a corner.

`query._apply_filters` already states the rule for the filter half: honoured or refused, never ignored.
A list that looks sorted and is not is the same lie, so a sort this query cannot serve now refuses.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_a_sort_is_honoured_or_refused
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api as smartview
from tatva_connect.tests.api import partner_fixture

NAME_KEY = "lead:first_name"
CREATION_KEY = "lead:creation"
MOBILE_KEY = "lead:mobile_no"
PHONE_PREFIX = "+91610008"
# Inserted in this order, so creation ASC is the exact reverse of the `modified DESC` fallback.
NAMES = ("ZZ Sort Alfa", "ZZ Sort Bravo", "ZZ Sort Charlie")
LABEL = "ZZ Sort View"


class TestASortIsHonouredOrRefused(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		partner_fixture.mint_grain()
		# The catalog is entitlement-bounded, so a contract THIS test owns grants the fields under test.
		cls.contract = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": "ZZ Sort Contract", "enabled": 1,
			"is_internal": 1, "vertical": partner_fixture.VERTICAL, "crm_group": partner_fixture.GROUP,
			"allowed_fields": [{"field": k} for k in (NAME_KEY, MOBILE_KEY, CREATION_KEY)],
		}).insert(ignore_permissions=True).name
		cls.leads = [cls._lead(f"{PHONE_PREFIX}{i:04d}", name) for i, name in enumerate(NAMES, start=1)]
		# PROJECTS the name and nothing else, so a sort on `creation` names a column the query has no other reason to resolve.
		cls.view = frappe.get_doc({
			"doctype": "CRM Smart View", "label": LABEL, "base_object": "Lead",
			"is_standard": 1, "vertical": partner_fixture.VERTICAL, "group": partner_fixture.GROUP,
			"columns": frappe.as_json([NAME_KEY]),
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		for name in frappe.get_all("CRM Smart View", filters={"label": LABEL}, pluck="name"):
			frappe.delete_doc("CRM Smart View", name, force=True, ignore_permissions=True)
		if frappe.db.exists("CRM Lead API Mapping", cls.contract):
			frappe.delete_doc("CRM Lead API Mapping", cls.contract, force=True, ignore_permissions=True)
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", f"{PHONE_PREFIX}%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	@classmethod
	def _lead(cls, phone, first_name):
		return frappe.get_doc({
			"doctype": "CRM Lead", "first_name": first_name, "mobile_no": phone, "status": "New",
			"custom_vertical": partner_fixture.VERTICAL, "custom_group": partner_fixture.GROUP,
		}).insert(ignore_permissions=True).name

	def _predicate(self, *conditions):
		"""Rewrite the view's predicate through the authoring door, so what is saved is what a person
		could have saved. The projection is resent because `upsert_view` materialises it on every save."""
		smartview.upsert_view({
			"name": self.view, "label": LABEL, "base_object": "Lead", "columns": [NAME_KEY],
			"predicate": {"op": "and", "conditions": list(conditions)} if conditions else None,
		})

	def _mine(self):
		"""The fixture's own leads and nothing else — the site's other leads are not this test's subject."""
		return {"field": MOBILE_KEY, "operator": "like", "value": PHONE_PREFIX}

	def _page(self, **kw):
		return smartview.get_data(self.view, page=1, page_size=200, **kw)

	def _order(self, rows):
		return [r["name"] for r in rows]

	def _by_creation(self):
		"""The fixture's leads in creation order, tie-broken by name exactly as the page query is."""
		rows = frappe.get_all("CRM Lead", filters={"name": ["in", self.leads]},
		                      fields=["name", "creation"])
		return [r.name for r in sorted(rows, key=lambda r: (r.creation, r.name))]

	# ---- the count keeps every term the predicate needs ---------------------

	def test_a_view_sorted_by_its_own_filtered_field_still_counts_its_rows(self):
		"""THE defect: the count dropped the sort key, so the predicate's own leaf could not resolve and
		the tab reported zero rows under a grid that was drawing them."""
		self._predicate({"field": NAME_KEY, "operator": "=", "value": NAMES[1]})
		out = self._page(sort=[NAME_KEY, "asc"], with_count=1)
		self.assertEqual(len(out["rows"]), 1, "the grid draws the one matching lead")
		self.assertEqual(out["total"], 1, "and the count must agree with it")

	def test_the_count_matches_the_page_whether_or_not_it_is_sorted(self):
		"""Metamorphic: a sort is a presentation choice, so it cannot move the number that MATCHED."""
		self._predicate({"field": NAME_KEY, "operator": "=", "value": NAMES[1]})
		self.assertEqual(self._page(with_count=1)["total"],
		                 self._page(sort=[NAME_KEY, "desc"], with_count=1)["total"])

	def test_a_two_condition_view_sorted_by_the_first_of_them_still_answers(self):
		"""The 500: the dropped key fell to `_never_matches()`, first in its group, and the fold raised
		TypeError before the count could run."""
		self._predicate({"field": NAME_KEY, "operator": "=", "value": NAMES[1]}, self._mine())
		out = self._page(sort=[NAME_KEY, "asc"], with_count=1)
		self.assertEqual(out["total"], 1)
		self.assertEqual(len(out["rows"]), 1)

	# ---- the sort itself ----------------------------------------------------

	def test_a_sort_on_a_column_the_view_does_not_project_is_honoured(self):
		"""THE defect: `creation` is a plain driving-row column needing no join, and the page still came
		back in `modified DESC` order because the key never reached the query's term set."""
		self._predicate(self._mine())
		got = self._order(self._page(sort=[CREATION_KEY, "asc"])["rows"])
		self.assertEqual(got, self._by_creation(), "the page must be ordered by the column asked for")
		self.assertNotEqual(got, list(reversed(self._by_creation())),
		                    "the fixture must not read the same both ways, or this proves nothing")

	def test_the_other_direction_is_honoured_too(self):
		"""`desc` and `asc` must differ, or the column is not deciding the order at all."""
		self._predicate(self._mine())
		self.assertEqual(self._order(self._page(sort=[CREATION_KEY, "desc"])["rows"]),
		                 list(reversed(self._by_creation())))

	def test_no_sort_still_falls_to_the_default(self):
		"""The behaviour that must NOT change: a page nobody sorted is still newest-first."""
		self._predicate(self._mine())
		self.assertEqual(self._order(self._page()["rows"]), list(reversed(self._by_creation())),
		                 "freshly inserted rows are `modified DESC` in reverse creation order")

	def test_a_sort_this_query_cannot_serve_is_refused(self):
		"""Honoured or refused, never ignored — the rule `_apply_filters` already states for filters."""
		self._predicate(self._mine())
		with self.assertRaises(frappe.ValidationError):
			self._page(sort=["lead:zz_not_a_catalog_field", "asc"])
