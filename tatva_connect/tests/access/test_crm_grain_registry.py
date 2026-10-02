# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""CRM Grain, the registry of valid (vertical, group, programme) tuples: one row per tuple, axes fixed once set,
and ensure_grains, which every grain patch reuses, inserts only what is missing. Own fixture masters."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.patches import backfill_crm_grain as backfill

V = "ZZ Test Vertical"
G = "ZZ Test Group"
P1 = "ZZ Test Program One"
P2 = "ZZ Test Program Two"


def _masters():
	for doctype, fieldname, value in (("CRM Vertical", "vertical_name", V), ("CRM Group", "group_name", G),
	                                  ("CRM Program", "program_name", P1), ("CRM Program", "program_name", P2)):
		if not frappe.db.exists(doctype, value):
			frappe.get_doc({"doctype": doctype, fieldname: value}).insert(ignore_permissions=True)


def _grain(program):
	return frappe.get_doc({"doctype": "CRM Grain", "vertical": V, "group": G, "program": program or None}).insert(
		ignore_permissions=True)


class TestOneRowPerTuple(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_masters()

	def test_a_duplicate_tuple_is_refused(self):
		_grain(P1)
		with self.assertRaises(frappe.DuplicateEntryError):
			_grain(P1)

	def test_a_rows_axes_cannot_drift_from_its_name(self):
		doc = _grain(P2)
		doc.program = P1
		with self.assertRaises(frappe.CannotChangeConstantError):
			doc.save(ignore_permissions=True)

	def test_a_blank_programme_is_its_own_row(self):
		self.assertEqual(_grain("").name, f"{V}::{G}::")


class TestEnsureGrainsIsIdempotent(IntegrationTestCase):
	"""ensure_grains commits, as patch code does, so these clean up through the document API."""

	def setUp(self):
		_masters()
		frappe.db.commit()  # the code under test commits; its fixtures must exist outside the rollback too
		self.addCleanup(self._purge)

	@staticmethod
	def _purge():
		for name in frappe.get_all("CRM Grain", filters={"vertical": V}, pluck="name"):
			frappe.delete_doc("CRM Grain", name, force=True, ignore_permissions=True)
		for doctype, name in (("CRM Program", P1), ("CRM Program", P2), ("CRM Group", G), ("CRM Vertical", V)):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()  # undo what the committed patch code left behind

	def test_a_replay_inserts_nothing(self):
		with patch.object(backfill, "GRAINS", [(V, G, P1), (V, G, "")]):
			self.assertEqual(len(backfill.ensure_grains()), 2)
			self.assertEqual(backfill.ensure_grains(), [])

	def test_a_tuple_whose_masters_are_absent_is_skipped_not_thrown(self):
		with patch.object(backfill, "GRAINS", [(V, G, "ZZ No Such Program")]):
			self.assertEqual(backfill.ensure_grains(), [])
