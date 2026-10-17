# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Agent Load: an agent in a team holding one open ticket shows under that team with one open ticket and no check-in."""
import frappe
from frappe.desk.form import assign_to
from frappe.tests import IntegrationTestCase
from frappe.utils import today

from tatva_connect.hd_reports.report.agent_load import agent_load
from tatva_connect.helpdesk import TICKET

TEAM = "ZZ Agent Load Team"
AGENT = "zz-agent-load@example.test"


class TestAgentLoad(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("agent_load")

	def tearDown(self):
		frappe.db.rollback(save_point="agent_load")

	def test_an_agents_open_ticket_counts_under_their_team(self):
		frappe.get_doc({"doctype": "User", "email": AGENT, "first_name": "Agent Load", "send_welcome_email": 0}).insert()
		frappe.get_doc({"doctype": "HD Agent", "user": AGENT, "agent_name": "Agent Load", "is_active": 1}).insert()
		frappe.get_doc({"doctype": "HD Team", "team_name": TEAM, "users": [{"user": AGENT}]}).insert()
		ticket = frappe.get_doc({"doctype": TICKET, "subject": "ZZ agent load fixture", "agent_group": TEAM}).insert()
		if not frappe.db.exists("ToDo", {"reference_type": TICKET, "reference_name": ticket.name, "allocated_to": AGENT, "status": "Open"}):
			assign_to.add({"assign_to": [AGENT], "doctype": TICKET, "name": ticket.name})
		[row] = agent_load.execute({"team": TEAM, "from_date": today(), "to_date": today()})[1]
		self.assertEqual((row["agent"], row["teams"], row["open_tickets"], row["resolved"], row["checkin"]), (AGENT, TEAM, 1, 0, "never"))
