# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The query brain (`automation/fields.py`) over the resource catalogs — one brain per resource, folded out
of the retired `CRM Automation Field`. What a workflow may reach is the GRAIN CONTRACT and nothing else;
routing derives from `CRM Lead Section`. `can_watch` is the one surviving flag and is not a permission —
it is the dispatcher's diff list. Real Frappe engine as the oracle; FrappeTestCase rolls the transaction
back, so toggling a real catalog flag in a test never persists.

C4 — a contract/catalog change is reflected by the reader with NO code edit.
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

	# C4 — toggle can_watch on the catalog, the reader reflects it with no code change.
	def test_can_watch_reflected_in_watchable(self):
		self._set(can_watch=0)
		self.assertFalse(fields.is_watchable("CRM Lead", self.row.fieldname))
		self.assertNotIn(self.row.fieldname, fields.watchable_fields("CRM Lead"))
		self._set(can_watch=1)
		self.assertTrue(fields.is_watchable("CRM Lead", self.row.fieldname))
		self.assertIn(self.row.fieldname, fields.watchable_fields("CRM Lead"))

	# Watching is not reaching: a watched field is still gated by its grain like any other.
	def test_can_watch_is_not_a_write_permission(self):
		self._set(can_watch=1)
		self.assertFalse(fields.is_settable("CRM Lead", self.row.fieldname, ("zz-no", "zz-no", "zz-no")))

	# Fail-closed: a grain no contract ticks reaches nothing, whatever the catalog says.
	def test_a_grain_with_no_contract_is_fenced(self):
		for row in frappe.get_all(_CATALOG, fields=["fieldname"], limit=5):
			self.assertFalse(
				fields.is_settable("CRM Lead", row.fieldname, ("zz-no", "zz-no", "zz-no")),
				f"{row.fieldname} was reachable at a grain no contract ticks",
			)

	# Fail-closed: a non-subject doctype has no catalog — no writes.
	def test_non_subject_doctype_is_fenced(self):
		self.assertFalse(fields.is_settable("Customer", "customer_name", ("", "", "")))


if __name__ == "__main__":
	unittest.main()
