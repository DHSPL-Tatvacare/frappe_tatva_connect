# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A ticket moves only as `HD Ticket Transition` says, and the rulebook is the only thing the code knows.

What is asserted:

  * with no rows the engine is DORMANT — a ticket moves exactly as stock helpdesk moves it;
  * a move the rulebook does not carry is refused, so a status with no outgoing row is final;
  * a move it carries is allowed;
  * a move reserved for a role is refused to whoever does not hold it, as a PermissionError;
  * a move is refused while a field it demands is empty, and allowed once that field is filled;
  * a transition may not demand a field the ticket does not have, and may not lead to its own status.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.helpdesk.test_transitions
"""
import frappe
from frappe.tests.utils import FrappeTestCase

TICKET = "HD Ticket"
STATUS = "HD Ticket Status"
TRANSITION = "HD Ticket Transition"

# Distinctive enough that no operator status can collide with them, so the fixture is unambiguous. A
# ticket's FIRST status is never chosen here: helpdesk stamps every new ticket with the site's default
# open status (hd_ticket.set_default_status), which is the PRD's "new tickets start as Open".
SECOND, THIRD = "ZZ Test Working", "ZZ Test Done"


class TestTicketTransitions(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		for label, category in ((SECOND, "Open"), (THIRD, "Resolved")):
			if not frappe.db.exists(STATUS, label):
				frappe.get_doc({
					"doctype": STATUS, "label_agent": label, "label_customer": label,
					"category": category, "color": "Blue", "enabled": 1,
				}).insert()

	def tearDown(self):
		frappe.db.rollback()

	def a_ticket(self):
		doc = frappe.get_doc({"doctype": TICKET, "subject": "Transition fixture",
		                      "via_customer_portal": 1}).insert()
		return frappe.get_doc(TICKET, doc.name)

	def start(self):
		"""The status helpdesk itself puts a new ticket in — what every rule here hangs off."""
		return frappe.db.get_single_value("HD Settings", "default_ticket_status") or self.a_ticket().status

	def a_move(self, to_status, allowed_role=None, demands=(), from_status=None):
		return frappe.get_doc({
			"doctype": TRANSITION, "from_status": from_status or self.start(), "to_status": to_status, "enabled": 1,
			"allowed_role": allowed_role,
			"required_fields": [{"fieldname": f} for f in demands],
		}).insert()

	def move(self, ticket, to_status):
		ticket.status = to_status
		ticket.save()

	def test_an_empty_rulebook_enforces_nothing(self):
		"""Dormant by default: the engine ships inert and the first row switches it on.

		The rulebook is emptied inside this test's own transaction rather than assumed empty: a site
		that has configured its lifecycle is the normal case, and a test that only passes on a fresh
		one proves nothing about the code."""
		for name in frappe.get_all(TRANSITION, filters={"enabled": 1}, pluck="name"):
			frappe.db.set_value(TRANSITION, name, "enabled", 0)
		self.assertFalse(frappe.db.count(TRANSITION, {"enabled": 1}))
		ticket = self.a_ticket()
		self.move(ticket, THIRD)
		self.assertEqual(frappe.db.get_value(TICKET, ticket.name, "status"), THIRD)

	def test_a_move_the_rulebook_carries_is_allowed_and_one_it_does_not_is_refused(self):
		self.a_move(SECOND)
		ticket = self.a_ticket()
		self.move(ticket, SECOND)
		self.assertEqual(frappe.db.get_value(TICKET, ticket.name, "status"), SECOND)
		# Nothing leads out of SECOND, which is how a status is made final.
		with self.assertRaises(frappe.ValidationError):
			self.move(frappe.get_doc(TICKET, ticket.name), THIRD)

	def test_a_move_reserved_for_a_role_is_refused_to_whoever_lacks_it(self):
		self.a_move(THIRD, allowed_role="Agent Manager")
		ticket = self.a_ticket()
		frappe.set_user(self.a_user_without("Agent Manager"))
		try:
			with self.assertRaises(frappe.PermissionError):
				self.move(frappe.get_doc(TICKET, ticket.name), THIRD)
		finally:
			frappe.set_user("Administrator")
		self.move(frappe.get_doc(TICKET, ticket.name), THIRD)
		self.assertEqual(frappe.db.get_value(TICKET, ticket.name, "status"), THIRD)

	def test_a_save_that_restates_the_same_status_is_not_a_move(self):
		"""Helpdesk saves a ticket for many reasons; only a real change of status is put to the rulebook."""
		self.a_move(SECOND)
		ticket = self.a_ticket()
		ticket.subject = "Edited, status untouched"
		ticket.save()
		self.assertEqual(frappe.db.get_value(TICKET, ticket.name, "subject"), "Edited, status untouched")

	def test_a_move_waits_until_the_fields_it_demands_are_filled(self):
		self.a_move(SECOND, demands=("ticket_type",))
		ticket = self.a_ticket()
		ticket.ticket_type = None
		with self.assertRaises(frappe.ValidationError):
			self.move(ticket, SECOND)
		fresh = frappe.get_doc(TICKET, ticket.name)
		fresh.ticket_type = frappe.get_all("HD Ticket Type", pluck="name", limit=1)[0]
		self.move(fresh, SECOND)
		self.assertEqual(frappe.db.get_value(TICKET, ticket.name, "status"), SECOND)

	def test_a_customer_reply_reopens_a_ticket_the_rulebook_would_refuse_an_agent(self):
		"""Helpdesk reopens a ticket itself when mail arrives. Refusing that move loses the customer's reply
		and stops the whole mailbox: the pull raised `Open to In Progress needs Sub Type` on 2026-09-27 and
		the reply never reached the ticket. An agent is still held to the demand."""
		self.a_move(SECOND, demands=("ticket_type",))
		ticket = self.a_ticket()
		with self.assertRaises(frappe.ValidationError):
			self.move(frappe.get_doc(TICKET, ticket.name), SECOND)

		reply = frappe.get_doc(TICKET, ticket.name)
		reply.flags.customer_reply = True
		reply.status = SECOND
		reply.save()
		self.assertEqual(frappe.db.get_value(TICKET, ticket.name, "status"), SECOND)

	def test_a_customer_reply_cannot_invent_a_move_but_is_never_refused(self):
		"""The demands are waived and the shape is kept, yet the save must still succeed: the reply is the
		point, and raising here kills the whole email pull. The undescribed move is dropped instead."""
		ticket = self.a_ticket()
		self.a_move(SECOND)
		reply = frappe.get_doc(TICKET, ticket.name)
		reply.flags.customer_reply = True
		reply.status = THIRD
		reply.save()
		self.assertEqual(frappe.db.get_value(TICKET, ticket.name, "status"), reply.get_doc_before_save().status)

	def test_a_customer_reply_to_a_final_ticket_is_kept_and_the_status_holds(self):
		"""Nothing leaves THIRD, and a customer may still write to it. The reply must land and the mailbox
		must keep running, so the move is dropped rather than refused: the ticket stays where it was."""
		self.a_move(THIRD)
		ticket = self.a_ticket()
		self.move(ticket, THIRD)

		reply = frappe.get_doc(TICKET, ticket.name)
		reply.flags.customer_reply = True
		reply.status = SECOND
		reply.subject = "Customer wrote again"
		reply.save()
		self.assertEqual(frappe.db.get_value(TICKET, ticket.name, "status"), THIRD)
		self.assertEqual(frappe.db.get_value(TICKET, ticket.name, "subject"), "Customer wrote again")

	def test_a_transition_may_not_demand_a_field_the_ticket_has_not_got(self):
		with self.assertRaises(frappe.ValidationError):
			self.a_move(SECOND, demands=("no_such_column",))

	def test_a_transition_may_not_lead_to_its_own_status(self):
		with self.assertRaises(frappe.ValidationError):
			self.a_move(self.start())

	def a_user_without(self, role):
		"""An enabled agent who does not hold `role` — the caller a reserved move must refuse."""
		for user in frappe.get_all("HD Agent", pluck="name"):
			if role not in frappe.get_roles(user) and frappe.db.get_value("User", user, "enabled"):
				return user
		self.skipTest(f"every agent on this bench holds {role}")
