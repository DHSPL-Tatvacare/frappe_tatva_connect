# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""CRM Grain — the valid-combination registry. LOGIC only, independent of the seed.

LAYER 1 of two, and the boundary matters. These tests build their OWN fixture masters and assert
behaviour; they never assert a real tuple count ("Anaya has 4 programs"), because the test database
carries no business data and such an assertion would pass by being empty — green while proving
nothing. The real-data claims (every lead grain is a registry row or a reported cleanup item; Anaya
resolves to exactly its four programmes) are LAYER 2: a live bench run recorded as Gate 0 evidence,
not a rolled-back test.

Note the explicit tearDown deletes: `ensure_grains` commits (it is a patch), so FrappeTestCase's
rollback does not undo it and each test must clean up after itself.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.access.test_crm_grain_registry
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.patches import backfill_crm_grain as backfill

# Fixture masters — prefixed so they cannot collide with real taxonomy on a populated bench.
V = "ZZ Test Vertical"
G = "ZZ Test Group"
P1 = "ZZ Test Program One"
P2 = "ZZ Test Program Two"

_LEAD_PREFIX = "ZZ-GRAIN-TEST-"


class TestCRMGrainRegistry(FrappeTestCase):
	def setUp(self):
		self._master("CRM Vertical", "vertical_name", V)
		self._master("CRM Group", "group_name", G)
		self._master("CRM Program", "program_name", P1)
		self._master("CRM Program", "program_name", P2)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.sql("DELETE FROM `tabCRM Lead` WHERE name LIKE %s", (_LEAD_PREFIX + "%",))
		frappe.db.sql("DELETE FROM `tabCRM Grain` WHERE vertical = %s", (V,))
		for doctype, name in (
			("CRM Program", P1), ("CRM Program", P2), ("CRM Group", G), ("CRM Vertical", V),
		):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()

	# ---- fixtures -------------------------------------------------------------------------

	def _master(self, doctype, fieldname, value):
		if frappe.db.exists(doctype, value):
			return
		doc = frappe.new_doc(doctype)
		doc.set(fieldname, value)
		doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user

	def _grain(self, vertical, group, program):
		doc = frappe.new_doc("CRM Grain")
		doc.vertical, doc.group, doc.program = vertical, group, program or None
		doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		return doc

	def _lead(self, suffix, vertical, group, program):
		"""A raw lead row. The reporter reads the lead table with SQL, so a SQL fixture tests exactly
		what it consumes — and avoids dragging the CRM Lead save hooks (including the grain stamp,
		which is the very thing Phase 2 changes) into a Phase 0 test."""
		frappe.db.sql(
			"""INSERT INTO `tabCRM Lead`
			   (name, creation, modified, modified_by, owner, docstatus, idx,
			    custom_vertical, custom_group, custom_current_program)
			   VALUES (%s, NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0, %s, %s, %s)""",
			(_LEAD_PREFIX + suffix, vertical, group, program or None),
		)

	# ---- (a) uniqueness -------------------------------------------------------------------

	def test_duplicate_triple_is_rejected(self):
		"""The composite name IS the uniqueness constraint — a second row for the same tuple cannot exist."""
		self._grain(V, G, P1)
		with self.assertRaises(frappe.DuplicateEntryError):
			self._grain(V, G, P1)

	def test_axes_are_set_only_once(self):
		"""Uniqueness is only true if a row's tuple cannot drift away from the name it was given."""
		doc = self._grain(V, G, P1)
		doc.reload()
		doc.program = P2
		with self.assertRaises(frappe.CannotChangeConstantError):
			doc.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user

	# ---- (b) blank program ----------------------------------------------------------------

	def test_blank_program_is_allowed_and_does_not_collide(self):
		"""A blank programme is a real combination (a lead exists before enrolment), and its composite
		name must not collide with the same group's concrete row."""
		blank = self._grain(V, G, "")
		concrete = self._grain(V, G, P1)
		self.assertNotEqual(blank.name, concrete.name)
		self.assertEqual(blank.name, f"{V}::{G}::")
		self.assertEqual(concrete.name, f"{V}::{G}::{P1}")
		self.assertIsNone(blank.program)
		self.assertEqual(frappe.db.count("CRM Grain", {"vertical": V}), 2)

	# ---- (c) backfill idempotency ---------------------------------------------------------

	def test_backfill_inserts_only_missing_and_replays_clean(self):
		"""Run twice: the second run inserts nothing and the row count is unchanged."""
		fixture = [(V, G, P1), (V, G, "")]
		with patch.object(backfill, "GRAINS", fixture):
			first = backfill.ensure_grains()
			self.assertEqual(len(first), 2)
			after_first = frappe.db.count("CRM Grain", {"vertical": V})

			second = backfill.ensure_grains()
			self.assertEqual(second, [], "replay must insert nothing")
			self.assertEqual(frappe.db.count("CRM Grain", {"vertical": V}), after_first)

	def test_backfill_fills_only_the_gap(self):
		"""One tuple already present -> only the other is inserted (not a blind re-insert of the set)."""
		self._grain(V, G, P1)
		with patch.object(backfill, "GRAINS", [(V, G, P1), (V, G, P2)]):
			inserted = backfill.ensure_grains()
		self.assertEqual(inserted, [f"{V}::{G}::{P2}"])

	def test_backfill_skips_a_tuple_whose_masters_are_absent(self):
		"""Fresh-site guard: a tuple naming a master that does not exist is skipped, not thrown on —
		otherwise it would fail the install before the taxonomy seed has run."""
		with patch.object(backfill, "GRAINS", [(V, G, "ZZ No Such Program")]):
			self.assertEqual(backfill.ensure_grains(), [])

	# ---- (d) the off-registry reporter ----------------------------------------------------

	def test_reporter_surfaces_a_lead_grain_absent_from_the_registry(self):
		"""Bad data must surface as a cleanup item instead of being enshrined into config."""
		self._grain(V, G, P1)
		self._lead("known", V, G, P1)
		self._lead("stray", V, G, P2)

		reported = {(r.vertical, r.group, r.program) for r in backfill.off_registry_lead_grains()}
		self.assertIn((V, G, P2), reported, "a lead on a non-registry grain must be reported")
		self.assertNotIn((V, G, P1), reported, "a lead on a registry grain must not be reported")

	def test_reporter_counts_the_leads_it_reports(self):
		self._lead("stray-1", V, G, P2)
		self._lead("stray-2", V, G, P2)
		hit = [r for r in backfill.off_registry_lead_grains() if (r.vertical, r.group, r.program) == (V, G, P2)]
		self.assertEqual(len(hit), 1)
		self.assertEqual(hit[0].leads, 2)
