# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A ticket says where it started, and an email ticket says it without being told.

What is asserted:

  * a ticket carrying an email account is stamped Email, because nothing else fills that column;
  * a source the caller already chose is left alone;
  * a ticket from no mailbox is left blank for the agent;
  * with no Email row on the site, the ticket is left blank rather than carrying an invented value.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.helpdesk.test_source
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.helpdesk import SOURCE, TICKET
from tatva_connect.helpdesk.source import EMAIL

OTHER = "ZZ Test Source"


class TestTicketSource(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		for label in (EMAIL, OTHER):
			if not frappe.db.exists(SOURCE, label):
				frappe.get_doc({"doctype": SOURCE, "__newname": label}).insert()
		self.mailbox = frappe.get_all("Email Account", filters={"enable_incoming": 1}, pluck="name")

	def tearDown(self):
		frappe.db.rollback()

	def a_ticket(self, **fields):
		doc = frappe.get_doc({"doctype": TICKET, "subject": "Source fixture",
		                      "via_customer_portal": 1, **fields}).insert()
		return frappe.get_doc(TICKET, doc.name)

	def test_a_ticket_from_a_mailbox_is_stamped_email(self):
		if not self.mailbox:
			self.skipTest("no incoming email account on this bench")
		ticket = self.a_ticket(email_account=self.mailbox[0])
		self.assertEqual(ticket.custom_ticket_source, EMAIL)

	def test_a_source_the_caller_chose_is_left_alone(self):
		if not self.mailbox:
			self.skipTest("no incoming email account on this bench")
		ticket = self.a_ticket(email_account=self.mailbox[0], custom_ticket_source=OTHER)
		self.assertEqual(ticket.custom_ticket_source, OTHER)

	def test_a_ticket_from_no_mailbox_is_left_for_the_agent(self):
		self.assertIsNone(self.a_ticket().custom_ticket_source)

	def test_no_email_row_means_blank_never_an_invented_value(self):
		if not self.mailbox:
			self.skipTest("no incoming email account on this bench")
		frappe.db.set_value(SOURCE, EMAIL, "disabled", 1)
		self.assertIsNone(self.a_ticket(email_account=self.mailbox[0]).custom_ticket_source)
