# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A check-in mirrors to Helpdesk as the first ENABLED status of its category, never a disabled one ordered ahead of it."""

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.lead import checkin
from tatva_connect.tests.workflow_engine import fixtures as fx


class TestACheckInMirrorsOnlyAnEnabledHelpdeskStatus(IntegrationTestCase):
	def setUp(self):
		if "helpdesk" not in frappe.get_installed_apps():
			self.skipTest("Helpdesk is not installed")
		switch = frappe.get_doc("CRM Tatva Automation", checkin.HELPDESK_MIRROR)
		switch.enabled = 1
		switch.save(ignore_permissions=True)
		self.agent = fx.rep("mirror-agent")
		frappe.get_doc({"doctype": "HD Agent", "user": self.agent, "agent_name": "Mirror Agent", "availability": "Away"}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "HD Agent Status", "agent_status": "ZZ Disabled First", "category": "Active", "status_order": 0, "enabled": 0}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()

	def test_the_mirror_skips_a_disabled_status(self):
		checkin.mirror_to_helpdesk(frappe._dict(user=self.agent, status="Active"))
		self.assertEqual(frappe.db.get_value("HD Agent", {"user": self.agent}, "availability"), "Active")
