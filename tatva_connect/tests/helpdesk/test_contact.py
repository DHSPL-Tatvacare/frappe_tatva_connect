# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A contact is the person, found by the number they carry.

What is asserted:

  * a number nobody holds creates a contact, storing it in E.164 and leaving the name blank;
  * the same number in another spelling finds that contact rather than making a second one;
  * a number that is not real is refused, so a typo never becomes a person.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.helpdesk.test_contact
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.helpdesk.contact import contact_for

NUMBER = "+919812397001"
SAME_NUMBER_WRITTEN_DIFFERENTLY = ("09812397001", "9812397001", "+91 9812397001")


class TestContactByPhone(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def test_a_new_number_creates_a_nameless_contact_in_e164(self):
		name = contact_for(NUMBER)
		contact = frappe.get_doc("Contact", name)
		self.assertEqual([p.phone for p in contact.phone_nos], [NUMBER])
		self.assertFalse(contact.first_name)

	def test_the_same_number_written_differently_finds_the_same_contact(self):
		first = contact_for(NUMBER)
		for spelling in SAME_NUMBER_WRITTEN_DIFFERENTLY:
			with self.subTest(spelling=spelling):
				self.assertEqual(contact_for(spelling), first)

	def test_a_number_that_is_not_real_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			contact_for("12")
