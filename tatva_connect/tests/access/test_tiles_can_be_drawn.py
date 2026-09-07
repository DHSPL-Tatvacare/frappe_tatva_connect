# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A dashboard tile is a REPORT-level read, so the doctype behind it must grant `report`.

THE DEFECT THIS LOCKS OUT, measured on a bench. The Observability desk answered `You don't have
permission to get a report on: CRM API Request Log` — to a System Manager. `db_query` refuses an
aggregate read without `report`, `report` is 0 across the ledger by design, and the tail rights that
ride a reader live in `EXTRA_PTYPES`. Fifteen tiles on that one page failed, and thirteen more
doctypes behind other desks were one click away from the same thing.

The list is DERIVED, never typed: every `document_type` a chart or a number card fixture names is a
doctype a tile reads, so adding a tile on a doctype that cannot be reported on fails here rather than
failing silently in front of whoever opens the page.

`report` is not a widening of WHAT a role sees — `permission_query_conditions` still decides the rows.
It is the right to ask the question in aggregate at all.
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
	def test_every_doctype_a_tile_reads_grants_report(self):
		missing = sorted(dt for dt in _tile_doctypes()
		                 if "report" not in ledger.extra_ptypes_for(dt))
		self.assertEqual(missing, [], f"tiles read these, but the ledger grants them no `report`: {missing}")

	def test_the_fixtures_actually_name_doctypes(self):
		"""Guards the guard: a fixture rename that emptied the set would make the test above vacuous."""
		self.assertGreater(len(_tile_doctypes()), 10)

	def test_every_tile_doctype_is_governed_by_the_ledger(self):
		"""A tile on an undeclared doctype resolves to DENIED, so the page would refuse it anyway."""
		undeclared = sorted(dt for dt in _tile_doctypes() if not ledger.is_declared(dt))
		self.assertEqual(undeclared, [], f"tiles read these, which the ledger never opened: {undeclared}")
