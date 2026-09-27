# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A sub type belongs to one ticket type, and the agent's picker is built from the rows that say so.

What is asserted:

  * a ticket may not pair a sub type with a type that does not own it, and may with the one that does;
  * a retired sub type is offered to nobody;
  * a sub type that names a priority hands it to the ticket, but only while the switch is on, only as the
    sub type changes, and never over the agent's own later choice;
  * the picker's snapshot is rebuilt when a sub type is added, retired or deleted, so it can never
    describe a list that no longer exists. The snapshot is helpdesk's own mechanism and the only one its
    agent screen reads (`desk/src/composables/formCustomisation.ts`), so a stale one silently hides a
    real sub type.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.helpdesk.test_classification
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.helpdesk import SETTINGS, SUB_TYPE, TICKET
from tatva_connect.helpdesk.classification import PRIORITY_FIELD, PRIORITY_SWITCH, mapping

TYPE = "HD Ticket Type"
PRIORITY = "HD Ticket Priority"
FIRST, SECOND = "ZZ Test Type One", "ZZ Test Type Two"
SUB_ONE, SUB_TWO = "ZZ Test Sub One", "ZZ Test Sub Two"


class TestClassification(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		for label in (FIRST, SECOND):
			if not frappe.db.exists(TYPE, label):
				frappe.get_doc({"doctype": TYPE, "__newname": label}).insert()
		for label, owner in ((SUB_ONE, FIRST), (SUB_TWO, SECOND)):
			if not frappe.db.exists(SUB_TYPE, label):
				frappe.get_doc({"doctype": SUB_TYPE, "__newname": label, "ticket_type": owner}).insert()

	def tearDown(self):
		frappe.db.rollback()

	def a_ticket(self):
		"""Always handed back fresh from the database: helpdesk stamps `key` with a UUID object, and
		re-saving the inserted object raises CannotChangeConstantError, which is a ValidationError and
		would let a test pass on the wrong exception."""
		doc = frappe.get_doc({"doctype": TICKET, "subject": "Classification fixture",
		                      "via_customer_portal": 1}).insert()
		return frappe.get_doc(TICKET, doc.name)

	def refusal(self, ticket):
		"""Save and hand back the refusal text, so a test asserts WHICH rule spoke."""
		with self.assertRaises(frappe.ValidationError) as caught:
			ticket.save()
		return str(caught.exception)

	def test_a_sub_type_of_another_type_is_refused_and_its_own_is_accepted(self):
		ticket = self.a_ticket()
		ticket.ticket_type = FIRST
		ticket.custom_ticket_sub_type = SUB_TWO
		self.assertIn("belongs to the ticket type", self.refusal(ticket))

		fresh = frappe.get_doc(TICKET, ticket.name)
		fresh.ticket_type = FIRST
		fresh.custom_ticket_sub_type = SUB_ONE
		fresh.save()
		self.assertEqual(frappe.db.get_value(TICKET, ticket.name, "custom_ticket_sub_type"), SUB_ONE)

	def test_changing_the_type_under_a_sub_type_is_refused_too(self):
		"""The pair breaks from either side: moving the type while the sub type sits still leaves a ticket
		whose sub type belongs to somebody else."""
		ticket = self.a_ticket()
		ticket.ticket_type = FIRST
		ticket.custom_ticket_sub_type = SUB_ONE
		ticket.save()

		fresh = frappe.get_doc(TICKET, ticket.name)
		fresh.ticket_type = SECOND
		self.assertIn("belongs to the ticket type", self.refusal(fresh))

	def test_a_renamed_sub_type_is_still_offered(self):
		"""A rename fires after_rename, not on_update: without it the picker keeps offering a name that is gone."""
		frappe.rename_doc(SUB_TYPE, SUB_ONE, "ZZ Test Sub One Renamed")
		offered = mapping()[FIRST]
		self.assertIn("ZZ Test Sub One Renamed", offered)
		self.assertNotIn(SUB_ONE, offered)

	def test_the_picker_offers_each_type_its_own_sub_types(self):
		offered = mapping()
		self.assertIn(SUB_ONE, offered[FIRST])
		self.assertNotIn(SUB_ONE, offered.get(SECOND, []))

	def test_the_picker_follows_a_sub_type_added_retired_and_deleted(self):
		"""The snapshot is derived, never maintained: this is what stops it describing a list that has moved on."""
		added = frappe.get_doc({"doctype": SUB_TYPE, "__newname": "ZZ Test Sub Three",
		                        "ticket_type": FIRST}).insert()
		self.assertIn(added.name, mapping()[FIRST])

		added.disabled = 1
		added.save()
		self.assertNotIn(added.name, mapping().get(FIRST, []))

		added.disabled = 0
		added.save()
		self.assertIn(added.name, mapping()[FIRST])

		frappe.delete_doc(SUB_TYPE, added.name)
		self.assertNotIn(added.name, mapping().get(FIRST, []))

	def a_priority_other_than(self, current):
		"""Read a priority off the master rather than naming one: the labels are the operator's to rename."""
		other = frappe.get_all(PRIORITY, filters={"name": ["!=", current]}, pluck="name", limit=1)
		if not other:
			self.skipTest("this bench has one ticket priority")
		return other[0]

	def declares_priority(self, sub_type, priority):
		frappe.db.set_value(SUB_TYPE, sub_type, PRIORITY_FIELD, priority)

	def the_switch(self, on):
		frappe.db.set_single_value(SETTINGS, PRIORITY_SWITCH, 1 if on else 0)

	def a_classified_ticket(self, sub_type):
		ticket = self.a_ticket()
		ticket.ticket_type = FIRST
		ticket.custom_ticket_sub_type = sub_type
		ticket.save()
		return frappe.get_doc(TICKET, ticket.name)

	def test_the_sub_type_hands_the_ticket_its_priority(self):
		wanted = self.a_priority_other_than(self.a_ticket().priority)
		self.the_switch(True)
		self.declares_priority(SUB_ONE, wanted)
		self.assertEqual(self.a_classified_ticket(SUB_ONE).priority, wanted)

	def test_the_switch_off_leaves_the_priority_alone(self):
		born_with = self.a_ticket().priority
		self.the_switch(False)
		self.declares_priority(SUB_ONE, self.a_priority_other_than(born_with))
		self.assertEqual(self.a_classified_ticket(SUB_ONE).priority, born_with)

	def test_a_sub_type_that_names_no_priority_leaves_it_alone(self):
		born_with = self.a_ticket().priority
		self.the_switch(True)
		self.declares_priority(SUB_ONE, None)
		self.assertEqual(self.a_classified_ticket(SUB_ONE).priority, born_with)

	def test_the_agent_has_the_last_word(self):
		"""The rule speaks as the sub type changes; a priority chosen afterwards is the agent's and stands."""
		self.the_switch(True)
		self.declares_priority(SUB_ONE, self.a_priority_other_than(self.a_ticket().priority))
		ticket = self.a_classified_ticket(SUB_ONE)
		chosen = self.a_priority_other_than(ticket.priority)
		ticket.priority = chosen
		ticket.save()
		self.assertEqual(frappe.db.get_value(TICKET, ticket.name, "priority"), chosen)
