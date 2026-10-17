# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Contact Ticket History lists exactly the tickets naming the contact, and refuses a user who may not read that contact.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.hd_reports.test_contact_ticket_history
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.hd_reports.report.contact_ticket_history.contact_ticket_history import execute

TICKET = "HD Ticket"


class TestContactTicketHistory(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _contact(self, name):
		return frappe.get_doc({"doctype": "Contact", "first_name": name}).insert().name

	def _ticket(self, contact):
		return frappe.get_doc({"doctype": TICKET, "subject": "History fixture", "via_customer_portal": 1, "contact": contact}).insert().name

	def test_only_the_contacts_tickets_and_an_outsider_is_refused(self):
		mine, other = self._contact("Zz History Mine"), self._contact("Zz History Other")
		expected = {self._ticket(mine), self._ticket(mine)}
		self._ticket(other)
		_columns, rows = execute({"contact": mine})
		self.assertEqual({r["name"] for r in rows}, expected)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			execute({"contact": mine})
