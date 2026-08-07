# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""file_name must not carry HTML — VAPT Report #14."""
import frappe
from frappe.tests.utils import FrappeTestCase


class TestFileNameSanitised(FrappeTestCase):

	def test_angle_brackets_are_stripped_from_file_name(self):
		doc = frappe.get_doc({
			"doctype": "File",
			"file_name": "<img src=x onerror=alert(1)>.png",
			"content": b"test",
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		stored = frappe.db.get_value("File", doc.name, "file_name")
		self.assertNotIn("<", stored, f"angle bracket survived in: {stored!r}")
		self.assertNotIn(">", stored, f"angle bracket survived in: {stored!r}")

	def test_clean_file_name_is_preserved(self):
		doc = frappe.get_doc({
			"doctype": "File",
			"file_name": "report.txt",
			"content": b"test2",
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		self.assertEqual(frappe.db.get_value("File", doc.name, "file_name"), "report.txt")
