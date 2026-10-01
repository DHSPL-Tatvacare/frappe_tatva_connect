# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The lifecycle gates a person's own pick and nothing else, and a status no gate names is open.

A permit list has to enumerate every move, so the pair nobody thought to write becomes a frozen ticket —
which is exactly how tickets came to sit in a status with no way out. A gate list cannot have that defect:
the absence of a rule is permission, so only the restrictions the business actually stated can refuse
anything.

What is asserted:

  * a status no gate names is reachable, from anywhere;
  * a gate's demanded fields refuse a person who left one empty, and let them through once filled;
  * a gate's role refuses a person who lacks it, and admits one who holds it;
  * a blank `from` gates that status from EVERY other status, not one;
  * an exact gate wins over the wildcard, so one route can be restricted while the rest stay open;
  * helpdesk answering mail is never judged — this is the defect that froze the tickets;
  * the workflow engine and a gated server lane are never judged either;
  * with no gate enabled anywhere the engine refuses nothing at all.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.helpdesk.test_only_a_person_is_judged
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.helpdesk import TICKET, TRANSITION
from tatva_connect.helpdesk.transitions import ANY_STATUS, CUSTOMER_REPLY, move_name
from tatva_connect.tests.authz.base import mk_user

AGENT = "zz-lifecycle-agent@example.com"
ROLE = "Agent Manager"
DISPOSITION = ["ticket_type", "custom_ticket_sub_type", "custom_resolution_reason"]


class TestOnlyAPersonIsJudged(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		self.statuses = frappe.get_all("HD Ticket Status", filters={"enabled": 1}, pluck="name")
		for needed in ("Open", "Resolved"):
			if needed not in self.statuses:
				self.skipTest(f"{needed} is not an enabled status on this bench")
		frappe.db.delete(TRANSITION)  # the rulebook under test is the one each case writes
		frappe.clear_cache(doctype=TRANSITION)

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_cache(doctype=TRANSITION)

	def a_gate(self, to_status, from_status=ANY_STATUS, role=None, demands=()):
		"""One restriction, through the document API so its own validate runs as it does for an operator."""
		gate = frappe.get_doc({
			"doctype": TRANSITION, "from_status": from_status, "to_status": to_status,
			"allowed_role": role, "enabled": 1,
			"required_fields": [{"fieldname": f} for f in demands],
		}).insert(ignore_permissions=True)
		frappe.clear_cache(doctype=TRANSITION)
		return gate

	def a_ticket(self, status="Open", **fields):
		doc = frappe.get_doc({"doctype": TICKET, "subject": "Lifecycle probe", **fields}).insert()
		if doc.status != status:
			frappe.db.set_value(TICKET, doc.name, "status", status, update_modified=False)
		return frappe.get_doc(TICKET, doc.name)

	def move(self, ticket, to_status, flag=None):
		"""Save the ticket into `to_status`; `flag` marks it as a lane the rulebook must not judge."""
		ticket.status = to_status
		if flag:
			ticket.flags[flag] = True
		ticket.save(ignore_permissions=True)
		return frappe.db.get_value(TICKET, ticket.name, "status")

	# --- the inversion -------------------------------------------------------

	def test_a_status_no_gate_names_is_open(self):
		self.a_gate("Resolved", demands=DISPOSITION)  # a rulebook exists, but not for In Progress
		if "In Progress" not in self.statuses:
			self.skipTest("In Progress is not enabled on this bench")
		self.assertEqual(self.move(self.a_ticket(), "In Progress"), "In Progress")

	def test_with_no_gate_enabled_nothing_is_refused(self):
		self.assertEqual(self.move(self.a_ticket(), "Resolved"), "Resolved")

	# --- what a gate does ----------------------------------------------------

	def test_a_demanded_field_refuses_a_person_who_left_it_empty(self):
		self.a_gate("Resolved", demands=DISPOSITION)
		with self.assertRaises(frappe.ValidationError):
			self.move(self.a_ticket(), "Resolved")

	def test_the_same_move_passes_once_the_fields_are_filled(self):
		self.a_gate("Resolved", demands=["custom_resolution_reason"])
		reason = frappe.get_all("HD Ticket Resolution Reason", pluck="name", limit=1)
		if not reason:
			self.skipTest("no resolution reason on this bench")
		ticket = self.a_ticket(custom_resolution_reason=reason[0])
		self.assertEqual(self.move(ticket, "Resolved"), "Resolved")

	def test_a_role_gate_refuses_a_person_without_it(self):
		self.a_gate("Closed" if "Closed" in self.statuses else "Resolved", role=ROLE)
		target = "Closed" if "Closed" in self.statuses else "Resolved"
		agent = mk_user(AGENT, ["Agent"])
		ticket = self.a_ticket()
		frappe.set_user(agent)
		try:
			with self.assertRaises(frappe.PermissionError):
				self.move(ticket, target)
		finally:
			frappe.set_user("Administrator")

	def test_a_role_gate_admits_a_person_who_holds_it(self):
		target = "Closed" if "Closed" in self.statuses else "Resolved"
		self.a_gate(target, role=ROLE)
		self.assertEqual(self.move(self.a_ticket(), target), target)  # Administrator holds every role

	# --- the wildcard --------------------------------------------------------

	def test_a_blank_from_gates_the_status_from_every_other_one(self):
		self.a_gate("Resolved", demands=["custom_resolution_reason"])
		self.assertTrue(frappe.db.exists(TRANSITION, move_name(ANY_STATUS, "Resolved")))
		for origin in [s for s in self.statuses if s not in ("Resolved", "Closed")]:
			with self.subTest(origin=origin), self.assertRaises(frappe.ValidationError):
				self.move(self.a_ticket(status=origin), "Resolved")

	def test_an_exact_gate_wins_over_the_wildcard(self):
		if "In Progress" not in self.statuses:
			self.skipTest("In Progress is not enabled on this bench")
		self.a_gate("Resolved", demands=["custom_resolution_reason"])
		self.a_gate("Resolved", from_status="In Progress", demands=[])
		self.assertEqual(self.move(self.a_ticket(status="In Progress"), "Resolved"), "Resolved")
		with self.assertRaises(frappe.ValidationError):
			self.move(self.a_ticket(status="Open"), "Resolved")

	# --- who is judged -------------------------------------------------------

	def test_helpdesk_answering_mail_is_never_judged(self):
		"""THE defect: a move the rulebook did not carry used to be silently reverted, freezing the ticket."""
		self.a_gate("Resolved", demands=DISPOSITION)
		self.assertEqual(self.move(self.a_ticket(), "Resolved", flag=CUSTOMER_REPLY), "Resolved")

	def test_the_workflow_engine_is_never_judged(self):
		self.a_gate("Resolved", demands=DISPOSITION)
		ticket = self.a_ticket()
		frappe.flags.in_workflow = True
		try:
			self.assertEqual(self.move(ticket, "Resolved"), "Resolved")
		finally:
			frappe.flags.in_workflow = False

	def test_the_partner_api_lane_is_never_judged(self):
		"""`in_partner_lane` reads `frappe.local.partner_ctx`, which the @_api preamble sets and nothing else does."""
		self.a_gate("Resolved", demands=DISPOSITION)
		ticket = self.a_ticket()
		frappe.local.partner_ctx = frappe._dict(probe=True)
		try:
			moved = self.move(ticket, "Resolved")
		finally:
			frappe.local.partner_ctx = None
		self.assertEqual(moved, "Resolved")

	def test_ignore_permissions_alone_does_not_switch_the_rulebook_off(self):
		"""The hole this engine must not have: `ignore_permissions` is ambient, so it may never grant an exemption."""
		self.a_gate("Resolved", demands=DISPOSITION)
		ticket = self.a_ticket()
		frappe.flags.ignore_permissions = True
		try:
			with self.assertRaises(frappe.ValidationError):
				self.move(ticket, "Resolved")
		finally:
			frappe.flags.ignore_permissions = False
