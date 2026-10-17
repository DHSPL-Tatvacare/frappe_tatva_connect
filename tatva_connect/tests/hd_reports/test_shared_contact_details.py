# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One phone held by two contacts shows once, whatever the spelling, naming the oldest contact.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.hd_reports.test_shared_contact_details
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.hd_reports.report.shared_contact_details.shared_contact_details import execute


def _contact(first, phone):
	contact = frappe.get_doc({"doctype": "Contact", "first_name": first})
	contact.append("phone_nos", {"phone": phone})
	return contact.insert(ignore_permissions=True).name


class TestSharedContactDetails(FrappeTestCase):
	def test_one_number_in_two_spellings_is_one_shared_value(self):
		first = _contact("Shared First", "+919876543219")
		second = _contact("Shared Second", "98765 43219")
		row = next(r for r in execute({"kind": "Phone"})[1] if first in r["contacts"].split(", "))
		self.assertEqual(row["contacts_count"], 2)
		self.assertIn(second, row["contacts"].split(", "))
		self.assertEqual(row["oldest_contact"], first)
