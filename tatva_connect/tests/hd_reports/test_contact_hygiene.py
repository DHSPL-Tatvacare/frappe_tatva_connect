# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Contact Hygiene counts a phone shared by two contacts once, comparing spellings on the last ten digits.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.hd_reports.test_contact_hygiene
"""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.hd_reports.report.contact_hygiene.contact_hygiene import execute

SHARED = "Phones on more than one contact"
MULTI = "Contacts holding more than one phone"


def _counts():
	return {r["problem"]: r["count"] for r in execute({})[1]}


class TestContactHygiene(IntegrationTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user("Administrator")

	def test_one_number_in_two_spellings_on_two_contacts_is_one_shared_phone(self):
		before = _counts()
		for first, phone in (("ZZ HDR One", "+91 90000 47213"), ("ZZ HDR Two", "9000047213")):
			frappe.get_doc({"doctype": "Contact", "first_name": first, "phone_nos": [{"phone": phone}]}).insert()
		after = _counts()
		self.assertEqual(after[SHARED], before[SHARED] + 1)
		self.assertEqual(after[MULTI], before[MULTI])
