# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Ticket Timeline shows an edit to the ticket it was asked for, and refuses a user who may not read that ticket.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.hd_reports.test_ticket_timeline
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.hd_reports.report.ticket_timeline.ticket_timeline import execute

TICKET = "HD Ticket"


class TestTicketTimeline(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_an_edit_shows_and_an_outsider_is_refused(self):
		ticket = frappe.get_doc({"doctype": TICKET, "subject": "Timeline fixture", "via_customer_portal": 1}).insert()
		ticket = frappe.get_doc(TICKET, ticket.name)
		ticket.subject = "Timeline fixture edited"
		ticket.save(ignore_version=False)  # frappe skips Version rows in tests unless asked (document.py:577)
		_columns, rows = execute({"ticket": ticket.name})
		edits = [r for r in rows if r["record_type"] == "Version"]
		self.assertTrue(any("Timeline fixture edited" in r["detail"] for r in edits))
		self.assertEqual(rows, sorted(rows, key=lambda r: r["when"], reverse=True))
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			execute({"ticket": ticket.name})
