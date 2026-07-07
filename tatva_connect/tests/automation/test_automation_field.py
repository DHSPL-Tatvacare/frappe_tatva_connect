# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The merged `CRM Automation Field` allowlist — validate() contract (fail-closed, unambiguous).

Replaces the retired test_watchable_field.py. Real Frappe engine as the oracle: real meta reads, real
validate() throws, no mocked verdicts. The contract: at least one capability; watch is grain-independent,
parent-only, subject-only (so grain/child columns only ever mean set-scope); a row key implies a child set.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

_DT = "CRM Automation Field"
_LEAD_FIELD = "custom_stage"            # Select on CRM Lead
_LEAD_DATE = "custom_last_report_date"  # Date on CRM Lead
_GRAIN = GRAINS[0]


class TestAutomationField(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()  # the grain axes are Links → the masters must exist

	def setUp(self):
		frappe.db.delete(_DT, {"doctype_name": ("in", ("CRM Lead", "CRM Task", "Customer"))})

	def tearDown(self):
		frappe.db.delete(_DT, {"doctype_name": ("in", ("CRM Lead", "CRM Task", "Customer"))})

	def _row(self, **kw):
		base = {"doctype": _DT, "doctype_name": "CRM Lead", "fieldname": _LEAD_FIELD, "enabled": 1}
		base.update(kw)
		return frappe.get_doc(base)

	# (a) a can_watch row (subject, blank grain) saves.
	def test_watch_row_saves(self):
		doc = self._row(can_watch=1).insert(ignore_permissions=True)
		self.assertTrue(frappe.db.exists(_DT, doc.name))

	# (b) a can_set row with a grain saves.
	def test_set_row_with_grain_saves(self):
		doc = self._row(fieldname=_LEAD_DATE, can_set=1, vertical=_GRAIN["vertical"]).insert(ignore_permissions=True)
		self.assertTrue(frappe.db.exists(_DT, doc.name))

	# (c) watch + set collapse to ONE row (both flags, one autoname).
	def test_watch_and_set_one_row(self):
		doc = self._row(can_watch=1, can_set=1).insert(ignore_permissions=True)
		self.assertTrue(doc.can_watch and doc.can_set)

	# (d) can_watch + a grain -> throws (watch is grain-independent — grain only means set-scope).
	def test_watch_with_grain_throws(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._row(can_watch=1, vertical=_GRAIN["vertical"]).insert(ignore_permissions=True)

	# (e) can_watch + child_table_field -> throws (a child row is not watchable).
	def test_watch_with_child_throws(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._row(can_watch=1, child_table_field="custom_lab_profile").insert(ignore_permissions=True)

	# (f) can_watch on a non-subject doctype -> throws.
	def test_watch_non_subject_throws(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._row(doctype_name="Customer", fieldname="customer_name", can_watch=1).insert(ignore_permissions=True)

	# (g) is_row_key without a settable child -> throws.
	def test_row_key_without_child_throws(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._row(fieldname=_LEAD_DATE, can_set=1, is_row_key=1).insert(ignore_permissions=True)

	# (h) neither capability -> throws (a row that does nothing is a config error).
	def test_no_capability_throws(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._row().insert(ignore_permissions=True)

	# (i) unknown fieldname -> throws (typo caught at config time, not at first misfire).
	def test_unknown_field_throws(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._row(fieldname="no_such_field", can_watch=1).insert(ignore_permissions=True)


if __name__ == "__main__":
	unittest.main()
