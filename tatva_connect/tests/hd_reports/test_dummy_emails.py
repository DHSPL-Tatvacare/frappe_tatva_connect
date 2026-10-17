# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Dummy Emails lists a contact's email that matches a pattern, names the pattern, and the filter replaces the default list.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.hd_reports.test_dummy_emails
"""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.hd_reports.report.dummy_emails.dummy_emails import execute

EMAIL = "zz-hdr-dummy@mailinator.com"


class TestDummyEmails(IntegrationTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user("Administrator")
		self.contact = frappe.get_doc({"doctype": "Contact", "first_name": "ZZ HDR Dummy",
			"email_ids": [{"email_id": EMAIL, "is_primary": 1}]}).insert().name

	def _hit(self, filters):
		return next((r for r in execute(filters)[1] if r["contact"] == self.contact), None)

	def test_a_default_pattern_catches_the_email_and_a_custom_list_replaces_it(self):
		hit = self._hit({})
		self.assertIsNotNone(hit)
		self.assertEqual((hit["email"], hit["pattern"], hit["is_primary"]), (EMAIL, "*@mailinator.com", 1))
		self.assertIsNone(self._hit({"patterns": "nobody-matches-this@*"}))
		self.assertEqual(self._hit({"patterns": "zz-hdr-*"})["pattern"], "zz-hdr-*")
