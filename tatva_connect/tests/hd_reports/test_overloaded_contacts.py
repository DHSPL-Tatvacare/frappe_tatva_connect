# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A contact holding two phones is overloaded; the same phone spelled with and without +91 counts once.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.hd_reports.test_overloaded_contacts
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.hd_reports.report.overloaded_contacts.overloaded_contacts import execute


def _contact(first, phones):
	contact = frappe.get_doc({"doctype": "Contact", "first_name": first})
	for phone in phones:
		contact.append("phone_nos", {"phone": phone})
	return contact.insert(ignore_permissions=True).name


class TestOverloadedContacts(FrappeTestCase):
	def test_two_numbers_overload_one_number_in_two_spellings_does_not(self):
		two = _contact("Overloaded Two", ["+919876543210", "+919876543211"])
		one = _contact("Overloaded One", ["+919876543212", "9876543212"])
		rows = {r["contact"]: r for r in execute({"which": "Phones"})[1]}
		self.assertEqual(rows[two]["phone_count"], 2)
		self.assertNotIn(one, rows)
