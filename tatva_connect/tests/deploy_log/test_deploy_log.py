# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An item's ID is logged once per site, so "Done is never run again" has one row to ask."""
import frappe
from frappe.tests.utils import FrappeTestCase

KEY = "seed:zz-deploy-log-test.py --once"


class TestTatvaDeployLog(FrappeTestCase):
	def setUp(self):
		frappe.delete_doc("Tatva Deploy Log", KEY, force=True, ignore_missing=True)

	def _log(self, skipped):
		return frappe.get_doc({"doctype": "Tatva Deploy Log", "key": KEY, "kind": "Seed", "skipped": skipped}).insert(
			ignore_permissions=True)

	def test_the_id_is_the_record(self):
		self.assertEqual(self._log(1).name, KEY)

	def test_the_same_id_is_refused_a_second_row(self):
		self._log(1)
		with self.assertRaises(frappe.DuplicateEntryError):
			self._log(0)
