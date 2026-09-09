# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A CELL READS AS A LABEL, AND THE COUNT IS ASKED ONCE.

Two things this suite locks, both found on a screen rather than in a log:

  * A Link to a grain master stores a COMPOSITE PRIMARY KEY, so a cell painted the raw
    `nivo_indication::Goodflip-Care::Anaya::::Melanoma` where every other surface in the CRM shows
    `Melanoma`. `get_data` now ships `<key>_label` BESIDE the untouched key, resolved through
    `taxonomy.labels` — the one title lookup. The key must survive: a row is what the client groups,
    filters and sorts by, and two values sharing a label across programmes are distinct keys, so
    replacing the key with the label would silently merge real rows.

  * Widening the page window cannot change how many rows MATCHED, yet Load More re-ran the whole
    COUNT on every press — the unbounded half of the call, while the rows are capped at PAGE_MAX.
    `with_count=0` skips it and answers `total: None`, which the client reads as "keep the last one".

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_cells_read_as_labels
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api as smartview


class TestCellsReadAsLabels(FrappeTestCase):
	def _a_lead_view(self):
		"""The first standard Lead view on this site, or skip — this asserts the composer, not a fixture."""
		name = frappe.db.get_value("CRM Smart View", {"base_object": "Lead", "is_standard": 1}, "name")
		if not name:
			self.skipTest("no standard Lead smart view on this site")
		return name

	def test_a_composite_key_ships_its_title_in_the_map_every_list_reads(self):
		"""Titles ride in `_link_titles` ({target}::{key} -> title), the map `api/list_link_titles` attaches
		to the native list, Kanban and group-by, and `tatva/linkTitle.js` reads on the client.

		This surface used to write a `<key>_label` beside each value instead — a second convention for one
		app's composite keys, which meant the Smart View cell could not be the cell every other list uses.
		The column carries its `options` so the cell knows which target to look the value up under."""
		view = self._a_lead_view()
		out = smartview.get_data(view)
		links = [c for c in out["columns"] if c["fieldtype"] == "Link"]
		if not links:
			self.skipTest("this view projects no Link column")
		titles = out.get("_link_titles") or {}
		titled = 0
		for row in out["rows"]:
			for column in links:
				value = row.get(column["key"])
				if not value:
					continue
				self.assertTrue(column["options"], f"{column['key']} is a Link and must name its target")
				# The map is driven by the FRAMEWORK's own flag, exactly as `list_link_titles` is: a target
				# that does not declare `show_title_field_in_link` is titled by the surface (a User reads
				# off the client's users store, as the native leads list does), not here.
				meta = frappe.get_meta(column["options"])
				if not (meta.show_title_field_in_link and meta.title_field):
					continue
				self.assertIn(f"{column['options']}::{value}", titles,
				              f"{column['key']} carries a value the title map does not cover")
				titled += 1
		if not titled:
			self.skipTest("no row on this view carries a Link value")

	def test_no_row_carries_the_old_label_convention(self):
		"""One mechanism, not two: nothing may go back to writing a second key beside the value."""
		out = smartview.get_data(self._a_lead_view())
		for row in out["rows"]:
			self.assertEqual([k for k in row if k.endswith("_label")], [],
			                 "a `<key>_label` is the second convention this consolidated away")

	def test_a_composite_key_is_never_replaced_by_its_label(self):
		"""The label rides alongside; swapping it in would merge two programmes' distinct keys into one row."""
		view = self._a_lead_view()
		out = smartview.get_data(view)
		for row in out["rows"]:
			for key, value in list(row.items()):
				if not key.endswith("_label"):
					continue
				base = key[: -len("_label")]
				self.assertIn(base, row, f"{key} shipped without the key it labels")
				if "::" in str(row[base]):
					self.assertNotEqual(row[base], value, "the key was overwritten with its own label")

	def test_the_count_is_skipped_when_only_the_window_widened(self):
		"""`with_count=0` answers `total: None` — Load More asks the same question a second time otherwise."""
		view = self._a_lead_view()
		counted = smartview.get_data(view, page_size=20)
		self.assertIsNotNone(counted["total"], "a first page must carry the count")
		widened = smartview.get_data(view, page_size=40, with_count=0)
		self.assertIsNone(widened["total"], "Load More must not pay for the count again")
		# The rows are still the real page — skipping the count skips NOTHING else.
		self.assertEqual(
			[r["name"] for r in widened["rows"][: len(counted["rows"])]],
			[r["name"] for r in counted["rows"]],
		)

	def test_the_count_still_answers_when_it_is_asked_for(self):
		"""The default is unchanged: a caller that says nothing still gets a count."""
		view = self._a_lead_view()
		self.assertIsNotNone(smartview.get_data(view)["total"])
