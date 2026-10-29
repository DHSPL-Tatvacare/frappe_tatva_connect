# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A ticket an agent raises in Helpdesk takes their grain, as a lead takes its rep's; an agent with no grain is refused, and the new-ticket form offers the grain they may file under."""

import frappe
from frappe.tests import set_user
from helpdesk.helpdesk.doctype.hd_ticket.api import new

from tatva_connect.helpdesk import TICKET
from tatva_connect.helpdesk.ticket import grain_choices
from tatva_connect.taxonomy import grain
from tatva_connect.tests.workflow_engine import fixtures as fx


class TestAnAgentsNewTicketTakesTheirGrain(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.entitled, cls.ungranted = fx.rep("tkt-grain", "Agent"), fx.rep("tkt-none", "Agent")
		fx.pool([(cls.entitled, 1, None)])  # pool membership is this suite's entitlement
		for user in (cls.entitled, cls.ungranted):
			frappe.get_doc({"doctype": "HD Agent", "user": user, "agent_name": user}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture

	def test_an_entitled_agent_is_offered_and_stamped_with_their_grain(self):
		axes = (fx.GRAIN["vertical"], fx.GRAIN["group"], fx.GRAIN["program"])
		with set_user(self.entitled):
			self.assertIn(dict(zip(grain.AXES, axes, strict=True)), grain_choices()["grains"])
			ticket = new({"subject": "ZZ grain ticket", "description": "raised by an agent"})
		self.assertEqual(grain.own_of(frappe.get_doc(TICKET, ticket.name)), axes)

	def test_an_agent_with_no_grain_is_refused(self):
		with set_user(self.ungranted), self.assertRaises(frappe.PermissionError):
			new({"subject": "ZZ grain ticket", "description": "raised by an agent"})
