# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""SLA Breaches: a ticket raised long ago and never answered shows as a missed first response with no reply and time overdue."""
import frappe
from frappe.tests import IntegrationTestCase, freeze_time

from tatva_connect.hd_reports.report.sla_breaches import sla_breaches
from tatva_connect.helpdesk import TICKET

RAISED = "2026-01-05 10:00:00"


class TestSlaBreaches(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("sla_breaches")

	def tearDown(self):
		frappe.db.rollback(save_point="sla_breaches")

	def test_an_unanswered_ticket_past_its_target_is_a_missed_first_response(self):
		with freeze_time(RAISED):
			ticket = frappe.get_doc({"doctype": TICKET, "subject": "ZZ SLA breach fixture"}).insert()
		if not ticket.sla:
			self.skipTest("no default SLA on this bench")
		rows = [r for r in sla_breaches.execute({"from_date": "2026-01-01", "to_date": "2026-01-31"})[1] if r["ticket"] == ticket.name]
		first = next(r for r in rows if r["missed"] == "First Response")
		self.assertEqual((first["due"], first["actual"]), (ticket.response_by, None))
		self.assertGreater(first["overdue_by"], 0)
