# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The SQL picklist grain clause and the Python `overlaps` rule select exactly the same rows.
The SQL is the only gate on get_list and report view, so a drift would leak another line's options."""
import itertools

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import picklist
from tatva_connect.taxonomy import grain as taxonomy_grain

_DOCTYPE = "CRM Picklist Value"

# Ours alone, so the matrix read can never pick up an operator's option.
CATEGORY = "zz_sql_twin_category"

# Per-axis distinct values: a clause that compares `group` against the vertical's value cannot pass.
_VERTICALS = ("", "ZZV1", "ZZV2")
_GROUPS = ("", "ZZG1", "ZZG2")
_PROGRAMS = ("", "ZZP1", "ZZP2")

# One value with a quote, on both sides, so frappe.db.escape is exercised by the same comparison.
_QUOTED = "ZZ'V3"

GRAINS = [*itertools.product(_VERTICALS, _GROUPS, _PROGRAMS), (_QUOTED, "", "")]


def _python_admits(row_grain, entitled_grain):
	"""The Python side of the rule, asked exactly as `entitlement` asks it."""
	candidate = dict(zip(taxonomy_grain.AXES, row_grain, strict=True))
	return taxonomy_grain.overlaps(candidate, *entitled_grain)


class TestPicklistGrainSqlTwin(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.made = []
		cls.token = {}
		for i, g in enumerate(GRAINS):
			token = f"zz-twin-{i:02d}"
			cls.token[g] = token
			doc = frappe.get_doc({
				"doctype": _DOCTYPE, "category": CATEGORY, "value": token, "display_label": token,
				"vertical": g[0], "group": g[1], "program": g[2],
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
			cls.made.append(doc.name)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in cls.made:
			if frappe.db.exists(_DOCTYPE, name):
				frappe.delete_doc(_DOCTYPE, name, force=True, ignore_permissions=True)
		super().tearDownClass()

	def _sql_admits(self, entitled_grain):
		"""The tokens the SQL clause actually selects, out of this fixture's rows only."""
		clause = picklist._grain_clause(entitled_grain)
		rows = frappe.db.sql(
			f"select `value` from `tab{_DOCTYPE}` where `category` = %s and ({clause})",
			(CATEGORY,), pluck=True,
		)
		return set(rows)

	def test_the_sql_clause_selects_exactly_what_the_python_rule_admits(self):
		"""Every (entitled grain, row grain) pair, with wildcards on both sides, gets the same answer."""
		for entitled in GRAINS:
			expected = {self.token[row] for row in GRAINS if _python_admits(row, entitled)}
			self.assertEqual(
				self._sql_admits(entitled), expected,
				f"SQL and taxonomy.grain.overlaps diverged for entitled grain {entitled}",
			)

	def test_a_fully_blank_entitlement_selects_every_row(self):
		"""A fully blank entitlement means ANY on every axis, so it selects every row."""
		self.assertEqual(self._sql_admits(("", "", "")), set(self.token.values()))

	def test_a_fully_named_entitlement_still_picks_up_the_global_rows(self):
		"""The other end: a named grain must see its own rows AND every wildcard row, never only exact hits."""
		selected = self._sql_admits(("ZZV1", "ZZG1", "ZZP1"))
		self.assertIn(self.token[("", "", "")], selected, "a global option must be visible in every grain")
		self.assertIn(self.token[("ZZV1", "ZZG1", "ZZP1")], selected)
		self.assertNotIn(self.token[("ZZV2", "ZZG1", "ZZP1")], selected)
		self.assertNotIn(self.token[("ZZV1", "ZZG2", "ZZP1")], selected)
		self.assertNotIn(self.token[("ZZV1", "ZZG1", "ZZP2")], selected)

	def test_a_quoted_axis_value_matches_literally_and_does_not_break_the_clause(self):
		"""Escaping is part of the rule: the quoted grain must select its own row and no other named one."""
		selected = self._sql_admits((_QUOTED, "", ""))
		self.assertIn(self.token[(_QUOTED, "", "")], selected)
		self.assertNotIn(self.token[("ZZV1", "", "")], selected)

	def test_a_malformed_grain_is_refused_by_both_sides_not_widened(self):
		"""A grain missing an axis is refused by both sides, never turned into a wider clause."""
		with self.assertRaises(ValueError):
			picklist._grain_clause(("ZZV1", "ZZG1"))
		with self.assertRaises(TypeError):
			taxonomy_grain.overlaps({"vertical": "ZZV1"}, "ZZV1", "ZZG1")
