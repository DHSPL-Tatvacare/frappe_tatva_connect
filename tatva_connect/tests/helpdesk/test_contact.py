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


class TestOnePersonOneContact(FrappeTestCase):
	"""Every way a contact is made reaches the same brain: a phone or an email finds the person, and what they lacked is added."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def test_a_new_email_is_added_to_the_contact_the_phone_found(self):
		name = contact_for("+919812397011")
		self.assertEqual(contact_for("+919812397011", email="one.person@example.test"), name)
		self.assertEqual(frappe.db.get_value("Contact Email", {"parent": name}, "email_id"), "one.person@example.test")

	def test_a_listed_sender_gets_no_contact_and_any_other_sender_does(self):
		from tatva_connect.helpdesk.contact import contact_for_sender

		settings = frappe.get_single("HD Settings")
		settings.custom_no_contact_emails = "No-Reply@App.example.test\n"
		settings.save()
		self.assertIsNone(contact_for_sender("App <no-reply@app.example.test>"))
		made = contact_for_sender("Asha Rao <asha.sender@example.test>")
		self.assertEqual(frappe.db.get_value("Contact", made, "first_name"), "Asha Rao")
		self.assertEqual(contact_for_sender("asha.sender@example.test"), made)

	def test_helpdesk_create_contact_finds_the_person_before_it_makes_one(self):
		from tatva_connect.helpdesk.contact import create_contact

		existing = contact_for("+919812397012")
		self.assertEqual(create_contact({"first_name": "Again", "phone": "+919812397012"}), existing)
		self.assertNotEqual(create_contact({"first_name": "New", "phone": "+919812397013"}), existing)

	def test_a_ticket_for_a_contact_names_the_contact_never_the_staff_who_raised_it(self):
		patient = contact_for("+919812397014", email="patient.raised@example.test")
		ticket = frappe.get_doc({"doctype": "HD Ticket", "subject": "Raised by fixture", "contact": patient}).insert()
		self.assertEqual(ticket.raised_by, "patient.raised@example.test")
		no_email = contact_for("+919812397015")
		self.assertFalse(frappe.get_doc({"doctype": "HD Ticket", "subject": "No email", "contact": no_email}).insert().raised_by)

	def test_a_workflow_writing_a_contact_reaches_the_lead_s_contact(self):
		from tatva_connect.automation import actions
		from tatva_connect.workflow_engine.tests import fixtures as fx

		lead = fx.make_lead(mobile_no="+919812397016")
		target = actions.resolve_target(frappe._dict(action_type="Update Field", target_doctype="Contact"), lead.name, None)
		self.assertEqual(target, ("Contact", contact_for("+919812397016")))
