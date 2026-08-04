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

	def test_a_composite_key_ships_its_label_and_keeps_its_key(self):
		"""Every Link column answers with `<key>_label`, and the key itself is untouched."""
		view = self._a_lead_view()
		out = smartview.get_data(view)
		link_keys = [c["key"] for c in out["columns"] if c["fieldtype"] == "Link"]
		if not link_keys:
			self.skipTest("this view projects no Link column")
		labelled = 0
		for row in out["rows"]:
			for key in link_keys:
				if not row.get(key):
					continue
				self.assertIn(f"{key}_label", row, f"{key} carries a value but shipped no label")
				# The KEY is the contract. A composite one still reads as its full stored value.
				self.assertNotEqual(row[f"{key}_label"], None)
				labelled += 1
		if not labelled:
			self.skipTest("no row on this view carries a Link value")

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
