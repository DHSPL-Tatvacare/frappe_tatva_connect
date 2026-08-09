# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Bulk load from a spreadsheet is closed everywhere except the one place the business needs it.

WHY IT IS THIS FLAG AND NOT A HIDDEN BUTTON. `DataImport.validate_doctype` refuses on a falsy
`allow_import` BEFORE it looks at any permission, and a System Manager does not bypass that line the way
`permissions.can_import` lets them bypass the `import` ptype. So the flag is the only lever that closes
the Desk importer as well as the SPA's menu, and it closes it for every role.

WHY CRM LEAD IS EXCLUDED, AND WHY THAT IS ASSERTED. The flag is doctype-wide: listing CRM Lead here
would kill the bulk lead load at Desk too, which is the one import the business actually runs. That
exclusion is the whole point of the list, so it is a test rather than a comment - a later edit that
"completes" IMPORT_OFF by adding the obvious missing doctype breaks a red test instead of a go-live.

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

	def test_the_bulk_lead_load_is_untouched(self):
		# The exclusion IS the contract; the flag is doctype-wide, so naming CRM Lead would close Desk too.
		self.assertNotIn("CRM Lead", lockdown.IMPORT_OFF)
		self.assertTrue(frappe.get_meta("CRM Lead").allow_import)

	def test_the_lock_is_idempotent(self):
		# It runs on every migrate; a Property Setter that could not be re-applied would throw on the second.
		lockdown.apply_import_lock()
		self.assertEqual(
			frappe.db.count("Property Setter", {"property": "allow_import", "doc_type": ("in", lockdown.IMPORT_OFF)}),
			len([d for d in lockdown.IMPORT_OFF if frappe.db.exists("DocType", d)]),
		)
