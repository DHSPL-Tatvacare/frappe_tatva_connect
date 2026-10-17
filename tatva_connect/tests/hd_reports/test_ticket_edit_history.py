# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Ticket Edit History reads one row per changed value, labelled from the ticket's schema, and refuses a user who may not read the ticket.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.hd_reports.test_ticket_edit_history
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.hd_reports.report.ticket_edit_history.ticket_edit_history import execute

TICKET = "HD Ticket"


class TestTicketEditHistory(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_a_changed_value_reads_before_and_after(self):
		ticket = frappe.get_doc({"doctype": TICKET, "subject": "History fixture", "via_customer_portal": 1}).insert()
		ticket = frappe.get_doc(TICKET, ticket.name)
		ticket.subject = "History fixture edited"
		ticket.save(ignore_version=False)  # frappe skips Version rows in tests unless asked (document.py:577)
		label = frappe.get_meta(TICKET).get_label("subject")
		_columns, rows = execute({"ticket": ticket.name, "field": label})
		self.assertIn({"section": "Ticket", "field": label, "before": "History fixture", "after": "History fixture edited"},
			[{k: r[k] for k in ("section", "field", "before", "after")} for r in rows])
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			execute({"ticket": ticket.name})
