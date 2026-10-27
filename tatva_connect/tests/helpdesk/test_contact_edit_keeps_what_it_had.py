# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Two contacts already sharing a phone can each still be edited, while a phone another contact holds is still refused when added."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.helpdesk import contact

SHARED, OTHERS = "+919812397101", "+919812397102"


class TestContactEditKeepsWhatItHad(IntegrationTestCase):
	def _contact(self, first_name, *phones):
		return frappe.get_doc({"doctype": "Contact", "first_name": first_name,
		                       "phone_nos": [{"phone": p} for p in phones]}).insert().name

	def test_a_shared_phone_never_blocks_its_own_save_and_a_new_one_still_does(self):
		one, two, other = self._contact("Zz Shared One", SHARED), self._contact("Zz Shared Two", SHARED), self._contact("Zz Other", OTHERS)
		for name in (one, two):  # Desk's Contact form asks this before every save
			self.assertIsNone(contact.existing_holder([SHARED], [], name))
		self.assertEqual(contact.existing_holder([SHARED, OTHERS], [], one), other)  # adding another contact's phone is still stopped
		self.assertIn(contact.existing_holder([SHARED], []), (one, two))  # a new contact with a held phone is still stopped
		contact.edit_contact(two, {"first_name": "Zz Renamed", "phone_nos": [{"phone": SHARED, "is_primary": 1}]})  # Helpdesk's Edit Contact
		self.assertEqual(frappe.db.get_value("Contact", two, "first_name"), "Zz Renamed")
		with self.assertRaises(frappe.ValidationError):
			contact.edit_contact(two, {"phone_nos": [{"phone": SHARED}, {"phone": OTHERS}]})
