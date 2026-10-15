# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Assign to User acts on any record the run holds, a pool included, and can leave a held record alone.

What is asserted:

  * a pool draws onto a ticket the run holds, through the same draw a lead uses;
  * ticked "Only if nobody holds it", a held ticket keeps its holder (RED before: the pool cleared and re-drew);
  * publish refuses a pool whose Assignment Rule is made for another doctype (RED before: nothing checked it);
  * a blank Write to is still the lead, so every saved pool on prod is judged as before.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.workflow_engine.test_assign_to_any_record
"""
import frappe
from frappe.cache_manager import clear_doctype_map
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions
from tatva_connect.lead import assignment
from tatva_connect.workflow_engine import refs, registry
from tatva_connect.workflow_engine.tests import fixtures as fx

_HOLDER = "wf-any-holder@example.invalid"
_MEMBER = "wf-any-member@example.invalid"


def _ticket_pool():
	return fx.make_pool([{"user": _MEMBER}], rule="Round Robin", document_type="HD Ticket",
	                    grain_vertical="", grain_group="", grain_program="")


def _pool_on_ticket(rule, only_if_unassigned=0):
	return frappe._dict(action_type="Assign to User", assignee_mode=actions.POOL,
	                    assignment_rule=rule, target_doctype="HD Ticket", only_if_unassigned=only_if_unassigned)


class TestAPoolOnAnyRecord(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		fx.roll_back_pools(self)
		self.addCleanup(clear_doctype_map, "Assignment Rule", "HD Ticket")
		fx.make_user(_HOLDER)
		fx.make_user(_MEMBER)
		self.ticket = frappe.get_doc({"doctype": "HD Ticket", "subject": "ZZ any-record probe"}).insert()
		self.context = {}
		actions.remember_wrote(self.context, "HD Ticket", self.ticket.name)

	def _run(self, action):
		said = actions._action_assign_to_user(action, None, self.context, (), None)
		return said, assignment.current_assignees("HD Ticket", self.ticket.name)

	def test_a_pool_draws_onto_the_ticket_the_run_holds(self):
		said, holders = self._run(_pool_on_ticket(_ticket_pool().name))
		self.assertEqual(holders, [_MEMBER], said)
		self.assertEqual(self.context[refs.OUTPUT], "assigned")

	def test_a_held_ticket_keeps_its_holder_when_only_if_unassigned(self):
		rule = _ticket_pool().name
		assignment.assign("HD Ticket", self.ticket.name, _HOLDER, notify=False, ignore_permissions=True)
		said, holders = self._run(_pool_on_ticket(rule, only_if_unassigned=1))
		self.assertEqual(holders, [_HOLDER], said)
		self.assertEqual((self.context["assigned_to"], self.context[refs.OUTPUT]), (_HOLDER, "assigned"))


class TestThePoolMatchesWriteTo(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		fx.roll_back_pools(self)
		self.addCleanup(clear_doctype_map, "Assignment Rule", "HD Ticket")
		fx.make_user(_MEMBER)
		self.lead_pool = fx.make_pool([{"user": _MEMBER}], rule="Round Robin").name

	def _problems(self, **config):
		context = {"subject": "CRM Call Log", "grain": dict(zip(("vertical", "group", "program"), fx.AXES, strict=True)),
		           "configs": {}, "triggers": [], "trigger": None}
		found = registry.validate_node("Assign to User", {"assignee_mode": actions.POOL, **config}, [],
		                               mode=registry.PUBLISH, graph_context=context)
		return [p for p in found if p["field"] == "assignment_rule"]

	def test_a_lead_pool_writing_to_a_ticket_is_refused(self):
		problems = self._problems(assignment_rule=self.lead_pool, target_doctype="HD Ticket")
		self.assertEqual(len(problems), 1, problems)
		self.assertIn("made for CRM Lead", problems[0]["message"])

	def test_a_blank_write_to_is_the_lead_and_passes(self):
		self.assertEqual(self._problems(assignment_rule=self.lead_pool), [])
