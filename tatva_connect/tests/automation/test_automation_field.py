# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The automation query brain (`automation/fields.py`) over the resource catalogs — one brain per resource,
folded out of the retired `CRM Automation Field`. Capabilities are Check flags on `CRM Lead API Field` /
`CRM Task Type Field`; routing derives from `CRM Lead Section`, grain from the internal contract. Real
Frappe engine as the oracle; FrappeTestCase rolls the transaction back, so toggling a real catalog flag in
a test never persists.

C4 — a capability change on the catalog is reflected by the reader with NO code edit.
C5 — the retired `CRM Automation Field` doctype (doc + table) is absent.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import fields

_CATALOG = "CRM Lead API Field"


class TestAutomationFieldBrain(FrappeTestCase):
	def setUp(self):
		# A stable lead catalog row to toggle; the rollback undoes every flag change we make.
		self.row = frappe.get_all(_CATALOG, fields=["name", "fieldname"], order_by="name", limit=1)[0]

	def _set(self, **flags):
		frappe.db.set_value(_CATALOG, self.row.name, flags)

	# C5 — the retired doctype is gone: doc AND physical table.
	def test_crm_automation_field_doctype_absent(self):
		self.assertFalse(frappe.db.exists("DocType", "CRM Automation Field"))
		self.assertFalse(frappe.db.table_exists("CRM Automation Field"))

	# C4 — tick can_read on the catalog, the reader reflects it with no code change.
	def test_can_read_reflected_in_readable_fields(self):
		self._set(can_read=0, can_watch=0)
		self.assertNotIn(self.row.fieldname, fields.readable_fields("CRM Lead"))
		self._set(can_read=1)
		self.assertIn(self.row.fieldname, fields.readable_fields("CRM Lead"))

	# can_watch IMPLIES can_read (folded in readable_fields, and nowhere else), and is watchable.
	def test_can_watch_implies_readable_and_watchable(self):
		self._set(can_read=0, can_watch=1)
		self.assertIn(self.row.fieldname, fields.readable_fields("CRM Lead"))
		self.assertTrue(fields.is_watchable("CRM Lead", self.row.fieldname))
		self.assertIn(self.row.fieldname, fields.watchable_fields("CRM Lead"))

	# Fail-closed: an unticked field is neither readable, watchable, nor settable.
	def test_unticked_field_is_fenced(self):
		self._set(can_read=0, can_watch=0, can_set=0)
		self.assertNotIn(self.row.fieldname, fields.readable_fields("CRM Lead"))
		self.assertFalse(fields.is_watchable("CRM Lead", self.row.fieldname))
		self.assertFalse(fields.is_settable("CRM Lead", self.row.fieldname, ("", "", "")))

	# Fail-closed: a non-subject doctype has no catalog — empty vocabulary, no writes.
	def test_non_subject_doctype_is_fenced(self):
		self.assertEqual(fields.readable_fields("Customer"), [])
		self.assertFalse(fields.is_settable("Customer", "customer_name", ("", "", "")))


if __name__ == "__main__":
	unittest.main()
