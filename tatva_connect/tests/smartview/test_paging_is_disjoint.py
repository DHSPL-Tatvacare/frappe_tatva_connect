# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every page of a view is disjoint, complete and stable.

THE DEFECT. The order was `modified desc` (or whatever column a rep sorted by) and nothing else. None of
those columns is unique — a bulk load stamps the same `modified` on thousands of rows — so rows that TIE
have no defined order, and the database is free to return them differently on each query. Pages are cut
by LIMIT/OFFSET over that order, so a tied row could be read on two pages, or on none. An export walks up
to the operator's whole ceiling that way, against a table people are still editing, and the file was
quietly wrong either way: `produce_export` guards against dropping the tail and had nothing to say about
this.

A unique last key fixes it, and `name` is the only column guaranteed to be one.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_paging_is_disjoint
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api as smartview

PAGE = 20
PAGES = 6


class TestPagingIsDisjoint(FrappeTestCase):
	def _a_view(self):
		for row in frappe.get_all("CRM Smart View", filters={"base_object": "Lead"}, fields=["name"]):
			if smartview.get_data(row.name, page_size=1)["total"] > PAGE * 2:
				return row.name
		return None

	def _walk(self, view):
		names = []
		for page in range(1, PAGES + 1):
			rows = smartview.get_data(view, page=page, page_size=PAGE, with_count=0, with_titles=0)["rows"]
			names += [r["name"] for r in rows]
			if len(rows) < PAGE:
				break
		return names

	def test_the_sort_column_really_does_tie(self):
		"""The precondition. Without ties there is nothing for a tiebreaker to decide, and this whole file
		would be asserting the weather."""
		tied = frappe.db.sql(
			"select count(*) from (select modified from `tabCRM Lead` group by modified having count(*) > 1) t"
		)[0][0]
		if not tied:
			self.skipTest("no two leads on this bench share a `modified`")
		self.assertGreater(tied, 0)

	def test_pages_do_not_repeat_a_row(self):
		"""RED without the tiebreaker whenever a tie straddles a page boundary."""
		view = self._a_view()
		if not view:
			self.skipTest("no Lead view on this bench holds more than two pages")
		names = self._walk(view)
		self.assertEqual(len(names), len(set(names)), "a row was read on more than one page")

	def test_the_same_walk_twice_reads_the_same_rows(self):
		"""Stability is the other half: an unstable order drops rows as surely as it repeats them."""
		view = self._a_view()
		if not view:
			self.skipTest("no Lead view on this bench holds more than two pages")
		self.assertEqual(self._walk(view), self._walk(view))

	def test_a_sorted_view_pages_disjointly_too(self):
		"""The explicit-sort branch needs the tiebreaker exactly as much as the default one."""
		view = self._a_view()
		if not view:
			self.skipTest("no Lead view on this bench holds more than two pages")
		names = []
		for page in range(1, PAGES + 1):
			rows = smartview.get_data(
				view, sort=frappe.as_json(["lead:lead_name", "asc"]),
				page=page, page_size=PAGE, with_count=0, with_titles=0,
			)["rows"]
			names += [r["name"] for r in rows]
			if len(rows) < PAGE:
				break
		self.assertEqual(len(names), len(set(names)), "a sorted view repeated a row across pages")
