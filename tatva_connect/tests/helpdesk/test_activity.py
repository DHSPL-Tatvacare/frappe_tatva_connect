# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What changed on a ticket reads on one timeline, whether the field is helpdesk's or ours.

What is asserted:

  * a value set on one of our fields is logged as helpdesk logs its own, in its words;
  * clearing it is logged too, so the timeline never goes quiet on a field that emptied;
  * helpdesk's own six keep their lines, because ours are added to theirs and never replace them.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.helpdesk.test_activity
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.helpdesk import SOURCE, TICKET

ACTIVITY = "HD Ticket Activity"
SOURCE_FIELD = "custom_ticket_source"


class TestTicketActivity(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		self.source = frappe.get_all(SOURCE, filters={"disabled": 0}, pluck="name", limit=1)
		if not self.source:
			self.skipTest("no ticket source on this bench")

	def tearDown(self):
		frappe.db.rollback()

	def a_ticket(self):
		doc = frappe.get_doc({"doctype": TICKET, "subject": "Activity fixture",
		                      "via_customer_portal": 1}).insert()
		return frappe.get_doc(TICKET, doc.name)

	def timeline(self, ticket):
		return frappe.get_all(ACTIVITY, filters={"ticket": ticket.name}, pluck="action")

	def test_a_value_set_on_our_field_is_logged_as_helpdesk_logs_its_own(self):
		ticket = self.a_ticket()
		ticket.set(SOURCE_FIELD, self.source[0])
		ticket.save()
		self.assertIn(f"set source to {self.source[0]}", self.timeline(ticket))

	def test_clearing_our_field_is_logged_too(self):
		ticket = self.a_ticket()
		ticket.set(SOURCE_FIELD, self.source[0])
		ticket.save()

		fresh = frappe.get_doc(TICKET, ticket.name)
		fresh.set(SOURCE_FIELD, None)
		fresh.save()
		self.assertIn("cleared source", self.timeline(ticket))

	def test_helpdesk_keeps_its_own_lines(self):
		"""Ours are added to theirs: a priority change still speaks, or we have replaced their logger."""
		ticket = self.a_ticket()
		other = frappe.get_all("HD Ticket Priority", filters={"name": ["!=", ticket.priority]},
		                       pluck="name", limit=1)
		if not other:
			self.skipTest("this bench has one ticket priority")
		ticket.priority = other[0]
		ticket.save()
		self.assertIn(f"set priority to {other[0]}", self.timeline(ticket))
