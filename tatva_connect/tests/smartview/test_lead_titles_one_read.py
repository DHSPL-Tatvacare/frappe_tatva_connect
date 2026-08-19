# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`_lead_titles` asks the row gate ONCE for a page, and answers exactly what asking per row answered.

THE DEFECT THIS LOCKS. The map was built by calling `resolve_title` per name, and that is a per-DOCUMENT
permission check: with the sales hierarchy armed, crm's `has_lead_permission` (org_hierarchy.py:74) runs
its OWN select for every name, and `_in_hierarchy` re-queries twice more because the `request_cache` it
imports is never applied. A 200-row page therefore cost hundreds of round trips and a 5,000-row export
tens of thousands. Measured on prod for a Sales Manager inside the hierarchy that put one export past the
120s gateway timeout, while the SAME export by Administrator completed — because `frappe.has_permission`
returns True at permissions.py:107 before it loads anything, so the privileged path never paid it.

`get_list` asks the same question — `build_match_conditions`, which is the same user-permission rule and
the same crm hook — once, for every name at once. The two must therefore agree, and this pins that:

  * a lead the caller may NOT read gets no title (the rewrite must not widen the gate);
  * the map equals the per-row map it replaces, for a restricted AND a privileged caller;
  * and the cost stops growing with the page, which is the whole reason it changed.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_lead_titles_one_read
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api.list_link_titles import resolve_title
from tatva_connect.smartview import api as smartview

REP = "zz-titles-rep@example.com"
OTHER = "zz-titles-other@example.com"
PREFIX = "ZZ Titles"

# What six names cost the batch read: a handful of framework lookups, and never one per name. Set well
# above the batch form (a few) and well below the per-row form (~5 per name), so it fails on the shape
# that matters and not on a framework that adds a lookup.
QUERY_BUDGET = 10


def _per_row_map(names):
	"""The implementation this replaced, kept HERE as the reference answer rather than in the app — a
	test that asserts a rewrite is equivalent has to hold the thing it claims to be equivalent to."""
	titles = {}
	for name in {n for n in names if n}:
		title = resolve_title(smartview.LEAD_DOCTYPE, name)
		if title is not None:
			titles[f"{smartview.LEAD_DOCTYPE}::{name}"] = title
	return titles


class TestLeadTitlesOneRead(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for email, first in ((REP, "Titles Rep"), (OTHER, "Titles Other")):
			if not frappe.db.exists("User", email):
				frappe.get_doc({"doctype": "User", "email": email, "first_name": first,
				                "send_welcome_email": 0, "roles": [{"role": "Sales User"}]}
				               ).insert(ignore_permissions=True)
		# Six the rep owns and three they do not: enough of each that a widening shows as a count, and
		# enough owned rows that a per-name cost would blow the budget below.
		cls.mine = [cls._lead(f"{PREFIX} Mine {i}", REP) for i in range(6)]
		cls.theirs = [cls._lead(f"{PREFIX} Theirs {i}", OTHER) for i in range(3)]
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in cls.mine + cls.theirs:
			if frappe.db.exists(smartview.LEAD_DOCTYPE, name):
				frappe.delete_doc(smartview.LEAD_DOCTYPE, name, force=True, ignore_permissions=True)
		for email in (REP, OTHER):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@staticmethod
	def _lead(first_name, owner):
		return frappe.get_doc({"doctype": smartview.LEAD_DOCTYPE, "first_name": first_name,
		                       "lead_owner": owner}).insert(ignore_permissions=True).name

	def test_a_lead_the_caller_cannot_read_gets_no_title(self):
		"""The gate, stated as the outcome that matters: no title for a row this caller may not open."""
		frappe.set_user(REP)
		titles = smartview._lead_titles(self.mine + self.theirs)
		for name in self.mine:
			self.assertIn(f"{smartview.LEAD_DOCTYPE}::{name}", titles,
			              "a lead the rep owns lost its title")
		for name in self.theirs:
			self.assertNotIn(f"{smartview.LEAD_DOCTYPE}::{name}", titles,
			                 "a lead the rep may NOT read was titled — the batch read widened the gate")

	def test_the_map_is_what_asking_per_row_answered(self):
		"""Equivalence over a set that spans the gate, for the caller the gate actually restricts."""
		frappe.set_user(REP)
		names = self.mine + self.theirs
		self.assertEqual(smartview._lead_titles(names), _per_row_map(names))

	def test_a_privileged_caller_loses_nothing(self):
		"""The other side of the same rule — the persona whose export DID complete."""
		frappe.set_user("Administrator")
		names = self.mine + self.theirs
		self.assertEqual(smartview._lead_titles(names), _per_row_map(names))

	def test_the_cost_does_not_grow_with_the_page(self):
		"""THE regression this exists for: six names must not cost six permission checks."""
		frappe.set_user(REP)
		smartview._lead_titles(self.mine[:1])  # warm meta and roles, which are per-request, not per-name
		with self.assertQueryCount(QUERY_BUDGET):
			smartview._lead_titles(self.mine)
