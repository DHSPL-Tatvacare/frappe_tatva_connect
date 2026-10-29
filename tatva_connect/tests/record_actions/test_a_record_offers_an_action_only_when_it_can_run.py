# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Record actions through their one door: an action is offered only while its switch is on and its settings are filled, and acting on a record needs write on it."""

import frappe
from frappe.tests import set_user

from tatva_connect.api import record_actions as door
from tatva_connect.record_actions import jira
from tatva_connect.tests.workflow_engine import fixtures as fx

TICKET = "HD Ticket"


class TestARecordOffersAnActionOnlyWhenItCanRun(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		cls.addClassCleanup(frappe.clear_document_cache, jira.SETTINGS, jira.SETTINGS)
		super().setUpClass()
		cls.ticket = frappe.get_doc({"doctype": TICKET, "subject": "ZZ record action", "description": "x"}).insert(ignore_permissions=True).name
		cls.outsider = fx.rep("action-outsider")  # a Sales User: no write on a ticket

	def _settings(self, **values):
		settings = frappe.get_single(jira.SETTINGS)
		settings.update(values)
		settings.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, rolled back with the class

	def _offered(self):
		return [a["key"] for a in door.actions(TICKET, self.ticket)]

	def test_offered_only_while_switched_on_and_set_up(self):
		fx.set_switch(jira.SWITCH, 0)
		self._settings(jira_site="https://zz-test.atlassian.net", jira_email="zz@example.invalid", jira_api_token="zz-not-a-token", jira_projects="ZZ")
		self.assertEqual(self._offered(), [], "offered while its switch is off")
		fx.set_switch(jira.SWITCH, 1)
		self.assertEqual(self._offered(), [jira.KEY])
		self._settings(jira_projects="")
		self.assertEqual(self._offered(), [], "offered with a setting missing")

	def test_acting_on_a_record_needs_write_on_it(self):
		with set_user(self.outsider):
			for call in (lambda: door.form(jira.KEY, TICKET, self.ticket), lambda: door.run(jira.KEY, TICKET, self.ticket, {})):
				self.assertRaises(frappe.PermissionError, call)
