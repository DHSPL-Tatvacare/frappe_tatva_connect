# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A SMART VIEW DOWNLOAD READS LIKE THE SCREEN — the same rule the list download already applies.

A Link at a grain master holds a composite key (`Inside-Sales::Follow Up`) and the grid renders the
label beside it. The Smart View producer wrote the raw cell, so the same lead exported from the leads
list and from a Smart View gave a manager two different spreadsheets, and only one of them said what
the CRM says. `list_export._cells_read_as_the_app_does` had solved this for the other download; this
locks the Smart View half onto the same answer (`labels.shown_at`).

WHY `shown_at` AND NOT `shown`. `labels.shown` resolves the Link target by asking
`get_meta(doctype).get_field(fieldname)`. A Smart View column already NAMES its target in the catalog,
and its `fieldname` may belong to a child table, so that lookup resolves nothing. Both entry points run
the one rule underneath.

The formula guard is deliberately NOT tested here — it belongs to the file rather than to this producer
and is locked in `tests/api/test_tabular.py`.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api
from tatva_connect.taxonomy import labels


class TestTheDownloadReadsLikeTheScreen(FrappeTestCase):
	def setUp(self):
		self.stage = frappe.db.get_value("CRM Lead Stage", {}, "name")
		if not self.stage or "::" not in self.stage:
			self.skipTest("no composite CRM Lead Stage row on this site")
		self.column = {"key": "lead:custom_substage", "fieldtype": "Link", "options": "CRM Lead Stage"}

	def test_a_composite_key_exports_as_its_label(self):
		out = api._export_cell(self.column, self.stage, {})
		self.assertNotIn("::", str(out), f"the raw key reached the file: {out}")
		self.assertEqual(out, labels.shown_at("CRM Lead Stage", self.stage))

	def test_the_export_agrees_with_every_other_surface(self):
		"""One rule, asked the one way — a second answer here is the defect this closes."""
		self.assertEqual(api._export_cell(self.column, self.stage, {}),
		                 labels.shown_at("CRM Lead Stage", self.stage))

	def test_a_column_at_a_plain_master_is_left_alone(self):
		"""Only a COMPOSITE master is relabelled: `lead_owner` is a Link at User and must stay a user id."""
		col = {"key": "lead:lead_owner", "fieldtype": "Link", "options": "User"}
		self.assertEqual(api._export_cell(col, "someone@example.com", {}), "someone@example.com")

	def test_a_non_link_cell_passes_through_even_when_it_looks_composite(self):
		col = {"key": "lead:lead_name", "fieldtype": "Data", "options": ""}
		self.assertEqual(api._export_cell(col, "Abohar::Punjab", {}), "Abohar::Punjab")

	def test_none_becomes_empty_rather_than_the_word_None(self):
		col = {"key": "lead:lead_name", "fieldtype": "Data", "options": ""}
		self.assertEqual(api._export_cell(col, None, {}), "")

	def test_one_label_read_per_distinct_value_per_column(self):
		"""A hundred thousand rows of six stages must cost six reads, not a hundred thousand."""
		seen = {}
		for _ in range(50):
			api._export_cell(self.column, self.stage, seen)
		self.assertEqual(len(seen), 1)
