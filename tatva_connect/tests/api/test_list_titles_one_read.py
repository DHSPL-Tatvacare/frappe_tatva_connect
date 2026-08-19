# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A list titles its Link cells in one read per target, and never names a record the caller cannot see.

THE DEFECT, FOUND ON PROD. Every list resolved `_link_titles` one value at a time, and for a target that
declares its own `has_permission` that is a per-DOCUMENT check: `frappe.has_permission(..., doc=value)`
loads the whole document to run it, and CRM Lead carries eleven child tables. The Task list, whose rows
are a Dynamic Link to a lead, cost 293 queries for 20 rows — 18 leads x (1 doc + 11 children + 1 probe).

WHAT IS PINNED HERE IS THE SPLIT, not a number. A gated target is asked once through `get_list`, which
puts the same row gate in the WHERE clause. An ungated master keeps the per-value path on purpose,
because it answers from the doc cache: N cached reads cost no queries once warm, where one `name in (...)`
would cost one on every call, for every user, for ever.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.api.test_list_titles_one_read
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import list_link_titles

LEAD = "CRM Lead"
STAGE = "CRM Lead Stage"


class TestListTitlesAreOneReadPerTarget(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.leads = frappe.get_all(LEAD, limit=25, pluck="name")

	def _queries(self, fn):
		"""Every statement one call issues, so a count means the same thing on a warm bench as a cold one."""
		seen = []
		original = frappe.db.__class__.sql

		def spy(self, query, *args, **kwargs):
			seen.append(" ".join(str(query).split()))
			return original(self, query, *args, **kwargs)

		frappe.db.__class__.sql = spy
		try:
			fn()
		finally:
			frappe.db.__class__.sql = original
		return seen

	def test_a_target_with_its_own_gate_is_read_once(self):
		"""THE defect. One statement against the target, however many values are asked for."""
		self.assertTrue(list_link_titles._is_row_gated(LEAD), "CRM Lead declares no has_permission hook")
		seen = self._queries(lambda: list_link_titles._titles_for(LEAD, set(self.leads)))
		reads = [q for q in seen if f"tab{LEAD}`" in q or f"tab{LEAD} " in q]
		self.assertEqual(len(reads), 1, f"{len(self.leads)} leads cost {len(reads)} reads of the lead table")

	def test_a_master_keeps_the_cached_per_value_path(self):
		"""Deliberate, not an oversight: batching a master would replace a free cached read with a query."""
		self.assertFalse(
			list_link_titles._is_row_gated(STAGE),
			"a master was claimed as row-gated — it would lose the doc cache",
		)

	def test_it_never_names_a_record_the_caller_cannot_read(self):
		"""The gate. `get_list` must withhold exactly what `has_permission` withheld, value for value."""
		actor = frappe.db.get_value("User", {"enabled": 1, "name": ["like", "%@%"]}, "name")
		hidden = [n for n in self.leads if not frappe.db.get_value(LEAD, n, "lead_owner") == actor]
		if not hidden:
			self.skipTest("every lead is this caller's own — the gate is proving nothing")

		frappe.set_user(actor)
		try:
			titled = list_link_titles._titles_for(LEAD, set(self.leads))
			readable = set(
				frappe.get_list(LEAD, filters={"name": ["in", self.leads]}, limit_page_length=0, pluck="name")
			)
			self.assertEqual(set(titled), readable, "the batched door and the row gate disagree")
		finally:
			frappe.set_user("Administrator")

	def test_both_doors_return_the_same_title_for_the_same_value(self):
		"""`resolve_title` still serves the single-document surface, so the two must never diverge."""
		batched = list_link_titles._titles_for(LEAD, set(self.leads))
		for name in self.leads:
			self.assertEqual(
				list_link_titles.resolve_title(LEAD, name),
				batched.get(name),
				f"{name}: the per-value door and the batched door disagree",
			)
