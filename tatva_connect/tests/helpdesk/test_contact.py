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
		name = contact_for(NUMBER, create=True)
		contact = frappe.get_doc("Contact", name)
		self.assertEqual([p.phone for p in contact.phone_nos], [NUMBER])
		self.assertFalse(contact.first_name)

	def test_the_same_number_written_differently_finds_the_same_contact(self):
		first = contact_for(NUMBER, create=True)
		for spelling in SAME_NUMBER_WRITTEN_DIFFERENTLY:
			with self.subTest(spelling=spelling):
				self.assertEqual(contact_for(spelling), first)

	def test_a_number_that_is_not_real_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			contact_for("12", create=True)


class TestOnePersonOneContact(FrappeTestCase):
	"""Every way a contact is made reaches the same brain: a phone or an email finds the person, and what they lacked is added."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def test_a_new_email_is_added_to_the_contact_the_phone_found(self):
		name = contact_for("+919812397011", create=True)
		self.assertEqual(contact_for("+919812397011", email="one.person@example.test"), name)
		self.assertEqual(frappe.db.get_value("Contact Email", {"parent": name}, "email_id"), "one.person@example.test")

	def test_a_listed_sender_gets_no_contact_and_any_other_sender_does(self):
		from tatva_connect.helpdesk.contact import contact_for_sender

		settings = frappe.get_single("HD Settings")
		settings.custom_no_contact_emails = "No-Reply@App.example.test\n"
		settings.save()
		self.assertIsNone(contact_for_sender("App <no-reply@app.example.test>", create=True))
		made = contact_for_sender("Asha Rao <asha.sender@example.test>", create=True)
		self.assertEqual(frappe.db.get_value("Contact", made, "first_name"), "Asha Rao")
		self.assertEqual(contact_for_sender("asha.sender@example.test"), made)

	def test_helpdesk_create_contact_finds_the_person_before_it_makes_one(self):
		from tatva_connect.helpdesk.contact import create_contact

		existing = contact_for("+919812397012", create=True)
		self.assertEqual(create_contact({"first_name": "Again", "phone": "+919812397012"}), existing)
		self.assertNotEqual(create_contact({"first_name": "New", "phone": "+919812397013"}), existing)

	def test_a_ticket_for_a_contact_names_the_contact_never_the_staff_who_raised_it(self):
		patient = contact_for("+919812397014", email="patient.raised@example.test", create=True)
		ticket = frappe.get_doc({"doctype": "HD Ticket", "subject": "Raised by fixture", "contact": patient}).insert()
		self.assertEqual(ticket.raised_by, "patient.raised@example.test")
		no_email = contact_for("+919812397015", create=True)
		self.assertFalse(frappe.get_doc({"doctype": "HD Ticket", "subject": "No email", "contact": no_email}).insert().raised_by)

	def test_a_workflow_writing_a_contact_reaches_the_lead_s_contact(self):
		from tatva_connect.automation import actions
		from tatva_connect.workflow_engine.tests import fixtures as fx

		lead = fx.make_lead(mobile_no="+919812397016")
		action = frappe._dict(action_type="Update Field", target_doctype="Contact", create_contact=1)  # the node allows making one
		target = actions.resolve_target(action, lead.name, None)
		self.assertEqual(target, ("Contact", contact_for("+919812397016")))


class TestEveryPathAsksItsOwnTick(FrappeTestCase):
	"""RED before: an inbox with Create Contact off, and a workflow node, both made a Contact for anyone new."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def test_a_sender_is_linked_when_known_and_made_only_when_the_inbox_allows(self):
		from tatva_connect.helpdesk.contact import contact_for_sender

		self.assertIsNone(contact_for_sender("New Person <new.person@example.test>"))
		self.assertFalse(frappe.db.exists("Contact Email", {"email_id": "new.person@example.test"}))
		made = contact_for_sender("New Person <new.person@example.test>", create=True)
		self.assertEqual(contact_for_sender("new.person@example.test"), made, "a known sender is linked with the tick off")

	def test_a_mailed_ticket_follows_its_inbox_create_contact_tick(self):
		from tatva_connect.helpdesk.ticket import TatvaHDTicket

		# Incoming and outgoing both off, so the inbox is saved without ever reaching a mail server.
		inbox = frappe.get_doc({"doctype": "Email Account", "email_account_name": "ZZ Contact Tick Inbox",
		                        "email_id": "zz.tick.inbox@example.test", "enable_incoming": 0, "enable_outgoing": 0}).insert()
		for tick, sender in ((0, "off.sender@example.test"), (1, "on.sender@example.test")):
			inbox.create_contact = tick
			inbox.save()
			ticket = frappe.new_doc("HD Ticket")
			ticket.update({"email_account": inbox.name, "raised_by": sender})
			TatvaHDTicket.set_contact(ticket)
			with self.subTest(create_contact=tick):
				self.assertEqual(bool(ticket.contact), bool(tick))

	def test_a_workflow_contact_field_is_made_only_when_the_node_allows_and_kept_on_a_miss(self):
		from tatva_connect.automation import actions

		ticket = frappe.new_doc("HD Ticket")
		ticket.contact = "zz-kept-contact"
		number = "+919812397021"
		self.assertEqual(actions._stored_key(ticket, "contact", number, ()), "zz-kept-contact")
		self.assertEqual(actions._stored_key(ticket, "contact", "", ()), "", "a blank value still clears the field")
		self.assertFalse(frappe.db.exists("Contact Phone", {"phone": number}))
		made = actions._stored_key(ticket, "contact", number, (), create_contact=True)
		self.assertEqual(frappe.db.get_value("Contact Phone", {"parent": made}, "phone"), number)

	def test_a_workflow_contact_made_from_a_phone_is_named_by_it_and_a_named_one_is_kept(self):
		"""RED before: a caller with no contact got one with a blank name and a random ID on the ticket."""
		from tatva_connect.automation import actions

		ticket = frappe.new_doc("HD Ticket")
		made = actions._stored_key(ticket, "contact", "9812397051", (), create_contact=True)
		self.assertEqual(made, "9812397051")
		self.assertEqual(frappe.db.get_value("Contact", made, "first_name"), "9812397051")
		named = contact_for("+919812397052", full_name="Asha Rao", create=True)
		self.assertEqual(actions._stored_key(ticket, "contact", "9812397052", (), create_contact=True), named)
		self.assertEqual(frappe.db.get_value("Contact", named, "first_name"), "Asha Rao")

	def test_a_node_writing_to_the_patients_contact_finds_one_and_makes_one_only_when_allowed(self):
		from tatva_connect.automation import actions
		from tatva_connect.workflow_engine.tests import fixtures as fx

		lead = fx.make_lead(mobile_no="+919812397031")
		off = frappe._dict(action_type="Update Field", target_doctype="Contact", create_contact=0)
		self.assertIsNone(actions._open_record_for("Contact", off, lead.name))
		on = frappe._dict(action_type="Update Field", target_doctype="Contact", create_contact=1)
		made = actions._open_record_for("Contact", on, lead.name)
		self.assertTrue(made)
		self.assertEqual(actions._open_record_for("Contact", off, lead.name), made, "a contact that exists is found with the tick off")

	def test_an_assign_node_aimed_at_a_missing_contact_goes_nobody_instead_of_failing(self):
		"""RED before: with no Contact and none allowed, Assign to User passed a nameless Contact to the assigner and failed."""
		from tatva_connect.automation import actions
		from tatva_connect.workflow_engine.tests import fixtures as fx

		lead = fx.make_lead(mobile_no="+919812397041")
		context = {}
		said = actions._action_assign_to_user(frappe._dict(action_type="Assign to User", target_doctype="Contact"), lead.name, context, (), None)
		self.assertEqual(context[actions.refs.OUTPUT], "nobody")
		self.assertIn("nobody was assigned", said)

