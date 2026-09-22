# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The partner ticket API, driven as a real partner key on the fixture grain: line fence, contact by phone, comment scope."""
import unittest

import frappe

from tatva_connect.api import partner_ticket
from tatva_connect.api._base import ACTION_CREATED
from tatva_connect.tests.api import partner_fixture

PARTNER = "ticket.fixture.partner@example.test"
TICKET = "HD Ticket"


class TestPartnerTicket(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		partner_fixture.mint_partner(PARTNER)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		partner_fixture.teardown()
		frappe.db.commit()

	def setUp(self):
		self._form = frappe.form_dict
		frappe.set_user(PARTNER)

	def tearDown(self):
		frappe.form_dict = self._form
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def hit(self, fn, **args):
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(args)
		fn()
		return dict(frappe.local.response)

	def create(self, **args):
		answer = self.hit(partner_ticket.ticket_create, **{"subject": "Fixture ticket", **args})
		self.assertEqual(answer.get("status"), "success", answer)
		self.assertEqual(answer["action"], ACTION_CREATED)
		return answer["data"]

	def an_off_line_ticket(self):
		frappe.set_user("Administrator")
		try:
			return frappe.get_doc({"doctype": TICKET, "subject": "Off-line ticket", "via_customer_portal": 1}).insert().name
		finally:
			frappe.set_user(PARTNER)

	def test_a_ticket_lands_on_the_callers_line(self):
		row = frappe.db.get_value(TICKET, self.create(mobile_no="+919812398001")["name"],
		                          ["custom_vertical", "custom_group"], as_dict=True)
		self.assertEqual((row.custom_vertical, row.custom_group), (partner_fixture.VERTICAL, partner_fixture.GROUP))

	def test_a_new_number_creates_a_nameless_contact_in_e164(self):
		contact = frappe.get_doc("Contact", self.create(mobile_no="9812398002")["contact"])
		self.assertEqual([p.phone for p in contact.phone_nos], ["+919812398002"])
		self.assertFalse(contact.first_name)

	def test_the_same_number_in_another_spelling_reuses_the_contact(self):
		first = self.create(mobile_no="+919812398003")["contact"]
		self.assertEqual(self.create(mobile_no="09812398003")["contact"], first)

	def test_email_is_where_replies_go_and_a_bad_number_is_refused(self):
		self.assertEqual(self.create(mobile_no="+919812398004", email="patient@example.test")["email"],
		                 "patient@example.test")
		answer = self.hit(partner_ticket.ticket_create, subject="Bad", mobile_no="12")
		self.assertEqual(answer.get("status"), "error", answer)

	def test_a_ticket_off_the_callers_line_is_not_found_and_not_listed(self):
		name = self.an_off_line_ticket()
		self.assertEqual(self.hit(partner_ticket.ticket_get, name=name)["error"]["code"], "not_found")
		listed = self.hit(partner_ticket.ticket_list, limit=200)["data"]["tickets"]
		self.assertNotIn(name, [t["name"] for t in listed])

	def test_a_comment_needs_a_ticket_on_the_callers_line(self):
		own = self.create(mobile_no="+919812398005")["name"]
		made = self.hit(partner_ticket.comment_create, ticket=own, content="<p>ok</p>")["data"]
		self.assertEqual((made["ticket"], made["commented_by"]), (own, PARTNER))
		refused = self.hit(partner_ticket.comment_create, ticket=self.an_off_line_ticket(), content="<p>no</p>")
		self.assertEqual(refused["error"]["code"], "not_found")

	def test_outside_the_partner_lane_helpdesk_still_refuses_a_non_agent_naming_a_contact(self):
		contact = self.create(mobile_no="+919812398006")["contact"]
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc({"doctype": TICKET, "subject": "Direct", "contact": contact}).insert(ignore_permissions=True)
