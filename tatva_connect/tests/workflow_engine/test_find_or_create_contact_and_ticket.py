# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Find or Create Contact and Find or Create Ticket: one person, one ticket per team, the call linked to it.

What is asserted, each from the 2026-10-15 call-ticket run:

  * a caller with no lead calling three times gets one contact and one ticket (was 6 tickets for one number);
  * a second call finds the first call's ticket and leaves its subject alone (2572 was rewritten 9 times);
  * a ticket an agent parked keeps its status and its owner (2577 was reopened and moved to another agent);
  * the same caller on two teams keeps two tickets;
  * each call is linked to its ticket through the call's own Dynamic Link table;
  * a callback set to Do not create makes neither a contact nor a ticket;
  * a ticket closed inside the window is found as it is, and outside it a new one is made.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.workflow_engine.test_find_or_create_contact_and_ticket
"""
import frappe
from frappe.cache_manager import clear_doctype_map
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions
from tatva_connect.lead import assignment
from tatva_connect.workflow_engine import refs
from tatva_connect.workflow_engine.tests import fixtures as fx

_CALLER = "9812397071"
_AGENT = "wf-ticket-agent@example.invalid"


def _save(doctype, name, **values):
	"""Change a fixture record through the document API, so it carries a shape production can produce."""
	doc = frappe.get_doc(doctype, name)
	doc.update(values)
	doc.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, an agent's own edit


def _master(doctype, name, **values):
	if not frappe.db.exists(doctype, name):
		frappe.get_doc({"doctype": doctype, "__newname": name, **values}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture
	return name


def _resolved(ticket, team):
	"""Close a ticket the way an agent does, carrying the classification a Resolved ticket must have."""
	kind = _master("HD Ticket Type", "ZZ Ticket Node Type")
	_save("HD Ticket", ticket, agent_group=team, ticket_type=kind,
	      custom_ticket_sub_type=_master("HD Ticket Sub Type", "ZZ Ticket Node Sub Type", ticket_type=kind),
	      custom_resolution_reason=_master("HD Ticket Resolution Reason", "ZZ Ticket Node Reason"), status="Resolved")


def _team(name):
	if not frappe.db.exists("HD Team", name):
		frappe.get_doc({"doctype": "HD Team", "team_name": name, "users": [{"user": fx.make_user(_AGENT)}]}).insert(
			ignore_permissions=True)  # authz-ok: tier-c — test fixture
	return name


class TestOnePersonOneTicket(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(clear_doctype_map, "Assignment Rule", "HD Ticket")
		self.team = _team("ZZ Ticket Node Team")

	def _call(self, number=_CALLER):
		return frappe.get_doc({
			"doctype": "CRM Call Log", "id": f"ZZ-TN-{frappe.generate_hash(length=10)}", "type": "Incoming",
			"status": "Completed", "from": number, "to": "8065106183", "telephony_medium": "Manual",
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture

	def _contact(self, call, create=actions.CREATE):
		context = {"crm_call_log.from": call.get("from")}
		actions._action_find_contact(frappe._dict(value_from="crm_call_log.from", if_not_found=create), None, context, (), call)
		return context.get("contact"), context[refs.OUTPUT]

	def _ticket(self, call, contact, team=None, create=actions.CREATE, **config):
		context = {"c.contact": contact}
		action = frappe._dict(contact_from="c.contact", team=team or self.team, if_not_found=create,
		                      reuse_categories=["Open", "Paused"],
		                      create_fields=[{"name": "subject", "mode": refs.LITERAL, "value": f"Call {call.name}"}], **config)
		actions._action_find_ticket(action, None, context, (), call)
		return context.get("ticket"), context[refs.OUTPUT]

	def _run(self, team=None):
		call = self._call()
		contact, _ = self._contact(call)
		return (call, *self._ticket(call, contact, team))

	def test_three_calls_from_a_number_with_no_lead_make_one_contact_and_one_ticket(self):
		tickets = {self._run()[1] for _ in range(3)}
		self.assertEqual(len(tickets), 1)
		self.assertEqual(frappe.db.count("Contact Phone", {"phone": f"+91{_CALLER}"}), 1)

	def test_a_second_call_finds_the_ticket_and_leaves_its_subject(self):
		first_call, ticket, output = self._run()
		self.assertEqual(output, actions.CREATED)
		_, again, output = self._run()
		self.assertEqual((again, output), (ticket, actions.FOUND))
		self.assertEqual(frappe.db.get_value("HD Ticket", ticket, "subject"), f"Call {first_call.name}")

	def test_a_parked_ticket_keeps_its_status_and_owner(self):
		_, ticket, _ = self._run()
		_save("HD Ticket", ticket, status="Replied")
		fx.make_user(_AGENT)
		assignment.assign("HD Ticket", ticket, _AGENT, notify=False, ignore_permissions=True)
		_, again, output = self._run()
		self.assertEqual((again, output), (ticket, actions.FOUND))
		self.assertEqual(frappe.db.get_value("HD Ticket", ticket, "status"), "Replied")
		self.assertEqual(assignment.current_assignees("HD Ticket", ticket), [_AGENT])

	def test_the_same_caller_on_two_teams_keeps_two_tickets(self):
		other = _team("ZZ Ticket Node Other Team")
		self.assertNotEqual(self._run()[1], self._run(team=other)[1])

	def test_each_call_is_linked_to_its_ticket(self):
		call, ticket, _ = self._run()
		links = frappe.get_all("Dynamic Link", filters={"parenttype": "CRM Call Log", "parent": call.name},
		                       fields=["link_doctype", "link_name"])
		self.assertIn({"link_doctype": "HD Ticket", "link_name": ticket}, links)

	def test_a_callback_set_to_do_not_create_makes_nothing(self):
		call = self._call(number="9812397072")
		contact, output = self._contact(call, create=actions.DO_NOT_CREATE)
		self.assertEqual((contact, output), (None, actions.NOT_FOUND))
		known, _ = self._contact(call)
		ticket, output = self._ticket(call, known, create=actions.DO_NOT_CREATE)
		self.assertEqual((ticket, output), (None, actions.NOT_FOUND))
		self.assertFalse(frappe.db.exists("HD Ticket", {"contact": known}))

	def test_a_ticket_closed_inside_the_window_is_found_as_it_is(self):
		_, ticket, _ = self._run()
		_resolved(ticket, self.team)
		call = self._call()
		contact, _ = self._contact(call)
		self.assertEqual(self._ticket(call, contact, reopen_within='{"hours": 48}'), (ticket, actions.FOUND_CLOSED))
		self.assertEqual(frappe.db.get_value("HD Ticket", ticket, "status"), "Resolved")
		fresh, output = self._ticket(call, contact)
		self.assertEqual(output, actions.CREATED)
		self.assertNotEqual(fresh, ticket)
