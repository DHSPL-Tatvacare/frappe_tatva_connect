# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A dashboard tile is a REPORT-level read, so every reader of the doctype behind it must hold `report`.

THE DEFECT THIS LOCKS OUT, measured on a bench. The Observability desk answered `You don't have
permission to get a report on: CRM API Request Log` — to a System Manager. `db_query` refuses an
aggregate read without `report`, `report` is 0 across the ledger by design, and the tail rights that
ride a reader live in `EXTRA_PTYPES`. Fifteen tiles on that one page failed, and thirteen more
doctypes behind other desks were one click away from the same thing.

The cure is one rule, not a list: `lockdown` gives `report` to every role it gives `read`, because a
report returns the SAME rows through the SAME query conditions and permlevels, grouped. Withholding it
hides a view, never a record; what actually removes data is `export`, which stays per-doctype.

These tests hold the rule where it is observable — the tiles this app ships. The doctypes are DERIVED
from the fixtures, so a new tile on a doctype the ledger never opened fails here rather than failing
in front of whoever opens the page.
"""
import json
import os
import unittest

import frappe

from tatva_connect.access import ledger

FIXTURES = ("dashboard_chart.json", "number_card.json")


def _tile_doctypes():
	"""Every doctype a shipped tile reads, from the fixtures themselves."""
	folder = os.path.join(frappe.get_app_path("tatva_connect"), "fixtures")
	found = set()
	for name in FIXTURES:
		with open(os.path.join(folder, name), encoding="utf-8") as fh:
			for row in json.load(fh):
				if row.get("document_type"):
					found.add(row["document_type"])
	return found


class TestTilesCanBeDrawn(unittest.TestCase):
	def test_every_reader_of_a_tile_doctype_can_report_on_it(self):
		"""The live matrix, not the declaration: this is what `db_query` will ask when a tile draws."""
		broken = []
		for doctype in sorted(_tile_doctypes()):
			for row in frappe.get_all("Custom DocPerm", filters={"parent": doctype, "permlevel": 0},
			                          fields=["role", "read", "report"]):
				if row.read and not row.report:
					broken.append(f"{doctype}:{row.role}")
		self.assertEqual(broken, [], f"these can read but cannot draw a tile: {broken}")

	def test_no_doctype_declares_report_for_itself(self):
		"""`report` rides every reader in lockdown; a per-doctype entry means the rule was forgotten."""
		declared = sorted(dt for dt in ledger.EXTRA_PTYPES if "report" in ledger.EXTRA_PTYPES[dt])
		self.assertEqual(declared, [], f"report is universal now, remove it from EXTRA_PTYPES: {declared}")

	def test_the_fixtures_actually_name_doctypes(self):
		"""Guards the guard: a fixture rename that emptied the set would make the test above vacuous."""
		self.assertGreater(len(_tile_doctypes()), 10)

	def test_every_tile_doctype_is_governed_by_the_ledger(self):
		"""A tile on an undeclared doctype resolves to DENIED, so the page would refuse it anyway."""
		undeclared = sorted(dt for dt in _tile_doctypes() if not ledger.is_declared(dt))
		self.assertEqual(undeclared, [], f"tiles read these, which the ledger never opened: {undeclared}")
