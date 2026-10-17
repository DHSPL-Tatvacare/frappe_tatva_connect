# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Ticket Visibility Check's verdict is frappe's own answer: a requester with no ticket role matches helpdesk's rule yet is still refused, as the engine refuses them.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.hd_reports.test_ticket_visibility_check
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.hd_reports.report.ticket_visibility_check.ticket_visibility_check import execute

TICKET = "HD Ticket"
REQUESTER = "zz-visibility.requester@example.test"


class TestTicketVisibilityCheck(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _answers(self, ticket, user):
		return {r["question"]: r["answer"] for r in execute({"ticket": ticket, "user": user})[1]}

	def test_the_verdict_is_frappes_not_helpdesks_rule(self):
		if not frappe.db.exists("User", REQUESTER):
			frappe.get_doc({"doctype": "User", "email": REQUESTER, "first_name": "Requester", "send_welcome_email": 0}).insert()
		ticket = frappe.get_doc({"doctype": TICKET, "subject": "Visibility fixture", "via_customer_portal": 1, "raised_by": REQUESTER}).insert().name
		for user in (REQUESTER, "Administrator"):
			oracle = "Yes" if frappe.has_permission(TICKET, "read", ticket, user=user) else "No"
			self.assertEqual(self._answers(ticket, user)["Can open the ticket"], oracle, user)
		answers = self._answers(ticket, REQUESTER)
		self.assertEqual((answers["Raised it"], answers["Their roles grant ticket read"]), ("Yes", "No"))
