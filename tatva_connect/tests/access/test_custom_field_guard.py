# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Custom Field fieldname must stay a legal SQL identifier even when a save skips validate.
Uses a shipped field and is refused before any write, so nothing changes."""

import frappe
from frappe.tests import IntegrationTestCase

_PAYLOAD = 'charts<img src="0" onerror=alert(document.cookie)>'


class TestCustomFieldGuard(IntegrationTestCase):
	def test_renaming_a_field_to_a_payload_with_validate_skipped_is_refused(self):
		cf = frappe.get_doc("Custom Field", "CRM Lead-custom_vertical")
		cf.fieldname = _PAYLOAD
		cf.flags.ignore_validate = True
		with self.assertRaises(frappe.ValidationError):
			cf.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Custom Field", "CRM Lead-custom_vertical", "fieldname"), "custom_vertical")
