# Copyright (c) 2026, TatvaCare and Contributors. See license.txt
"""Link cells ship titles in `_link_titles` beside their untouched keys, and `with_count=0` answers `total: None`."""
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
		"""Every titled Link value is in `_link_titles` as {target}::{key}, and its column names the target in `options`."""
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
				# Only targets declaring `show_title_field_in_link` are titled server-side, as in `list_link_titles`.
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
		"""An export window's `with_count=0` answers `total: None` and skips nothing else."""
		view = self._a_lead_view()
		counted = smartview.get_data(view, page_size=20)
		self.assertIsNotNone(counted["total"], "a first page must carry the count")
		widened = smartview.get_data(view, page_size=40, with_count=0)
		self.assertIsNone(widened["total"], "a window that skips the count must not pay for it")
		# The rows are still the real page — skipping the count skips NOTHING else.
		self.assertEqual(
			[r["name"] for r in widened["rows"][: len(counted["rows"])]],
			[r["name"] for r in counted["rows"]],
		)

	def test_the_count_still_answers_when_it_is_asked_for(self):
		"""The default is unchanged: a caller that says nothing still gets a count."""
		view = self._a_lead_view()
		self.assertIsNotNone(smartview.get_data(view)["total"])
