# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A sub type belongs to one ticket type, and the agent's picker is built from the rows that say so.

What is asserted:

  * a ticket may not pair a sub type with a type that does not own it, and may with the one that does;
  * a retired sub type is offered to nobody;
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

from tatva_connect.helpdesk import SUB_TYPE, TICKET
from tatva_connect.helpdesk.classification import mapping

TYPE = "HD Ticket Type"
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
		return frappe.get_doc({"doctype": TICKET, "subject": "Classification fixture",
		                       "via_customer_portal": 1}).insert()

	def test_a_sub_type_of_another_type_is_refused_and_its_own_is_accepted(self):
		ticket = self.a_ticket()
		ticket.ticket_type = FIRST
		ticket.custom_ticket_sub_type = SUB_TWO
		with self.assertRaises(frappe.ValidationError):
			ticket.save()

		fresh = frappe.get_doc(TICKET, ticket.name)
		fresh.ticket_type = FIRST
		fresh.custom_ticket_sub_type = SUB_ONE
		fresh.save()
		self.assertEqual(frappe.db.get_value(TICKET, ticket.name, "custom_ticket_sub_type"), SUB_ONE)

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
