# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A channel account says which team owns its tickets, and the ticket inherits it on arrival.

What is asserted:

  * a ticket from a mailbox with a routing row lands on that team, so helpdesk's own rotation can assign it;
  * a mailbox with no row leaves the ticket as it is, unassigned, exactly as today;
  * a disabled row is no row;
  * a team the caller already chose is left alone;
  * a row that names neither a team nor a source is refused: it would do nothing;
  * the source a row names is written too, and one the caller already chose is left alone;
  * mail with no row, or a row that names no source, still says it came by mail;
  * a retired source is stamped on nothing.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.helpdesk.test_routing
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.helpdesk import ROUTING, SOURCE, TICKET
from tatva_connect.helpdesk.routing import EMAIL_ACCOUNT, MAIL_SOURCE

TEAM = "ZZ Test Routed Team"
OTHER_TEAM = "ZZ Test Other Team"
MAIL = MAIL_SOURCE


class TestTicketRouting(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		self.mailbox = frappe.get_all("Email Account", filters={"enable_incoming": 1}, pluck="name")
		if not self.mailbox:
			self.skipTest("no incoming email account on this bench")
		agent = frappe.get_all("HD Agent", filters={"is_active": 1}, pluck="name", limit=1)
		if not agent:
			self.skipTest("no active helpdesk agent on this bench")
		for team in (TEAM, OTHER_TEAM):
			if not frappe.db.exists("HD Team", team):
				# a team carries its members: that list is what helpdesk syncs into the team's rotation
				frappe.get_doc({"doctype": "HD Team", "team_name": team,
				                "users": [{"user": agent[0]}]}).insert()

	def tearDown(self):
		frappe.db.rollback()

	def a_route(self, **fields):
		"""This bench routes its real mailbox already, so the row under test replaces it for the length of
		this transaction; the rollback in tearDown puts the operator's row back."""
		name = f"{EMAIL_ACCOUNT}::{self.mailbox[0]}"
		if frappe.db.exists(ROUTING, name):
			frappe.delete_doc(ROUTING, name, force=True)
		return frappe.get_doc({"doctype": ROUTING, "account_doctype": EMAIL_ACCOUNT,
		                       "account": self.mailbox[0], "enabled": 1, **fields}).insert()

	def a_ticket(self, **fields):
		doc = frappe.get_doc({"doctype": TICKET, "subject": "Routing fixture", "via_customer_portal": 1,
		                      "email_account": self.mailbox[0], **fields}).insert()
		return frappe.get_doc(TICKET, doc.name)

	def test_a_ticket_inherits_the_team_its_mailbox_names(self):
		self.a_route(agent_group=TEAM)
		self.assertEqual(self.a_ticket().agent_group, TEAM)

	def test_a_mailbox_with_no_row_leaves_the_ticket_unassigned(self):
		name = f"{EMAIL_ACCOUNT}::{self.mailbox[0]}"
		if frappe.db.exists(ROUTING, name):
			frappe.delete_doc(ROUTING, name, force=True)
		self.assertIsNone(self.a_ticket().agent_group)

	def test_a_disabled_row_is_no_row(self):
		route = self.a_route(agent_group=TEAM)
		frappe.db.set_value(ROUTING, route.name, "enabled", 0)
		self.assertIsNone(self.a_ticket().agent_group)

	def test_a_team_the_caller_chose_is_left_alone(self):
		self.a_route(agent_group=TEAM)
		self.assertEqual(self.a_ticket(agent_group=OTHER_TEAM).agent_group, OTHER_TEAM)

	def test_a_ticket_inherits_the_source_its_mailbox_names(self):
		source = frappe.get_all("HD Ticket Source", filters={"disabled": 0}, pluck="name", limit=1)
		if not source:
			self.skipTest("no ticket source on this bench")
		self.a_route(ticket_source=source[0])
		self.assertEqual(self.a_ticket().custom_ticket_source, source[0])

	def test_a_source_the_caller_chose_is_left_alone(self):
		source = frappe.get_all("HD Ticket Source", filters={"disabled": 0}, pluck="name", limit=2)
		if len(source) < 2:
			self.skipTest("need two ticket sources on this bench")
		self.a_route(ticket_source=source[0])
		self.assertEqual(self.a_ticket(custom_ticket_source=source[1]).custom_ticket_source, source[1])

	def test_mail_with_no_row_still_says_it_came_by_mail(self):
		name = f"{EMAIL_ACCOUNT}::{self.mailbox[0]}"
		if frappe.db.exists(ROUTING, name):
			frappe.delete_doc(ROUTING, name, force=True)
		self.assertEqual(self.a_ticket().custom_ticket_source, MAIL)

	def test_a_row_that_names_no_source_falls_back_to_mail(self):
		self.a_route(agent_group=TEAM)
		self.assertEqual(self.a_ticket().custom_ticket_source, MAIL)

	def test_a_retired_source_is_stamped_on_nothing(self):
		"""The fallback names a master row, and a master row can be taken out of use."""
		frappe.db.set_value(SOURCE, MAIL, "disabled", 1)
		self.assertIsNone(self.a_ticket().custom_ticket_source)

	def test_a_row_that_says_nothing_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.a_route()
