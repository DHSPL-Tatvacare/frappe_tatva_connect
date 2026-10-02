# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every role that can read a doctype behind a shipped dashboard tile also holds `report`, so the tile draws.
The doctypes come from the fixtures, so a new tile on an unopened doctype fails here, not on the page."""
import json
import os

import frappe
from frappe.tests import IntegrationTestCase

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


class TestTilesCanBeDrawn(IntegrationTestCase):
	def test_every_reader_of_a_tile_doctype_can_report_on_it(self):
		"""The live matrix, not the declaration: this is what `db_query` will ask when a tile draws."""
		broken = []
		for doctype in sorted(_tile_doctypes()):
			for row in frappe.get_all("Custom DocPerm", filters={"parent": doctype, "permlevel": 0},
			                          fields=["role", "read", "report"]):
				if row.read and not row.report:
					broken.append(f"{doctype}:{row.role}")
		self.assertEqual(broken, [], f"these can read but cannot draw a tile: {broken}")

	def test_every_tile_doctype_is_governed_by_the_ledger(self):
		"""A tile on an undeclared doctype resolves to DENIED, so the page would refuse it anyway."""
		undeclared = sorted(dt for dt in _tile_doctypes() if not ledger.is_declared(dt))
		self.assertEqual(undeclared, [], f"tiles read these, which the ledger never opened: {undeclared}")
