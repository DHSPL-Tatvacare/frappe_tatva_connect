# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Bulk load through frappe's Data Import is closed for every listed doctype, CRM Lead included.
`allow_import` is checked before any permission, so it refuses every role, System Manager too."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import lockdown


class TestImportLock(IntegrationTestCase):
	def setUp(self):
		# Cleared first, or the tests pass on a setter a previous migrate left behind.
		frappe.db.delete("Property Setter", {"property": "allow_import", "doc_type": ("in", lockdown.IMPORT_OFF)})
		frappe.clear_cache()
		lockdown.apply_import_lock()

	def test_every_listed_doctype_refuses_a_data_import(self):
		for doctype in lockdown.IMPORT_OFF:
			if not frappe.db.exists("DocType", doctype):
				continue
			self.assertFalse(
				frappe.get_meta(doctype).allow_import,
				f"{doctype} still allows import — Data Import would accept it at Desk",
			)

	def test_a_crm_lead_data_import_is_refused(self):
		"""CRM Lead loads through Lead Import; frappe's Data Import would write leads past their contract."""
		doc = frappe.get_doc({"doctype": "Data Import", "reference_doctype": "CRM Lead", "import_type": "Insert New Records"})
		with self.assertRaises(frappe.ValidationError) as caught:
			doc.insert()
		self.assertIn("not allowed", str(caught.exception))

