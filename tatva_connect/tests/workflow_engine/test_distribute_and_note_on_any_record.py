# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Distribute and Create Note act on any record the run holds; a blank Write to is still the lead.

What is asserted:

  * Distribute gives a ticket the run holds to its pool (RED before: Distribute only ever acted on the lead);
  * publish refuses Reassign after on anything but the lead, since the hand-off moves a CRM Task with the lead
    (RED before: no such check);
  * Reassign after with a blank Write to still publishes, so every saved Distribute on prod is judged as before;
  * Create Note writes onto a ticket the run holds (RED before: it only ever wrote on the lead).

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.workflow_engine.test_distribute_and_note_on_any_record
"""
import frappe
from frappe.cache_manager import clear_doctype_map
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions
from tatva_connect.lead import assignment
from tatva_connect.workflow_engine import refs, registry
from tatva_connect.workflow_engine.tests import fixtures as fx

_MEMBER = "wf-dist-member@example.invalid"


class TestOnATicketTheRunHolds(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		fx.roll_back_pools(self)
		self.addCleanup(clear_doctype_map, "Assignment Rule", "HD Ticket")
		fx.make_user(_MEMBER)
		self.ticket = frappe.get_doc({"doctype": "HD Ticket", "subject": "ZZ distribute probe"}).insert()
		self.context = {}
		actions.remember_wrote(self.context, "HD Ticket", self.ticket.name)

	def test_distribute_gives_the_ticket_to_its_pool(self):
		rule = fx.make_pool([{"user": _MEMBER}], rule="Round Robin", document_type="HD Ticket",
		                    grain_vertical="", grain_group="", grain_program="").name
		action = frappe._dict(action_type="Distribute", assignment_rule=rule, target_doctype="HD Ticket")
		said = actions._action_distribute(action, None, self.context, (), None)
		self.assertEqual(assignment.current_assignees("HD Ticket", self.ticket.name), [_MEMBER], said)
		self.assertEqual(self.context[refs.OUTPUT], "assigned")

	def test_create_note_writes_on_the_ticket(self):
		action = frappe._dict(action_type="Create Note", comment_mode=refs.LITERAL, comment_text="Missed call 14:07",
		                      target_doctype="HD Ticket")
		actions._action_add_comment(action, None, self.context, (), None)
		self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": "HD Ticket", "reference_name": self.ticket.name,
		                                             "comment_type": "Comment", "content": "Missed call 14:07"}))


class TestReassignAfterIsLeadOnly(FrappeTestCase):
	def _codes(self, **config):
		found = registry.validate_node("Distribute", {"assignment_rule": "", "reassign_after": "{\"hours\": 1}", **config}, [])
		return {p["code"] for p in found if p["field"] == "reassign_after"}

	def test_reassign_after_on_a_ticket_is_refused(self):
		self.assertIn("field.wrong-target", self._codes(target_doctype="HD Ticket"))

	def test_reassign_after_with_a_blank_write_to_is_the_lead_and_passes(self):
		self.assertNotIn("field.wrong-target", self._codes())
