# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Bulk load through frappe's Data Import is closed for every listed doctype, CRM Lead included.

WHY IT IS THIS FLAG AND NOT A HIDDEN BUTTON. `DataImport.validate_doctype` refuses on a falsy
`allow_import` BEFORE it looks at any permission, and a System Manager does not bypass that line the way
`permissions.can_import` lets them bypass the `import` ptype. So the flag is the only lever that refuses
the save for every role.

CRM LEAD IS LISTED. It loads through Lead Import, which writes every row through its contract; frappe's
importer would write the same rows past it.

Run:
    bench --site dev.localhost run-tests --module tatva_connect.tests.access.test_import_lock
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import lockdown


class TestImportLock(FrappeTestCase):
	def setUp(self):
		# Cleared FIRST, or every assertion below passes on a setter a previous migrate left behind and proves nothing about the code under test.
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
		"""RED before: CRM Lead was left importable, so frappe's Data Import wrote leads past their contract."""
		doc = frappe.get_doc({"doctype": "Data Import", "reference_doctype": "CRM Lead", "import_type": "Insert New Records"})
		with self.assertRaises(frappe.ValidationError) as caught:
			doc.insert()
		self.assertIn("not allowed", str(caught.exception))

	def test_the_lock_is_idempotent(self):
		# It runs on every migrate; a Property Setter that could not be re-applied would throw on the second.
		lockdown.apply_import_lock()
		self.assertEqual(
			frappe.db.count("Property Setter", {"property": "allow_import", "doc_type": ("in", lockdown.IMPORT_OFF)}),
			len([d for d in lockdown.IMPORT_OFF if frappe.db.exists("DocType", d)]),
		)
