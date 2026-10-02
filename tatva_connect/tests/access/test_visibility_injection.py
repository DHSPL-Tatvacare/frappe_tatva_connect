# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A username carrying SQL never widens the row-visibility clause: run as real SQL, it returns only that literal
user's tasks, which is none, while the table holds tasks."""

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import visibility

SWITCH = "Task::CRM Task::visibility"
INJECTED = ("x' OR '1'='1", "x') OR ('1'='1", "x\\' OR 1=1 -- ")


class TestAnInjectedUsernameNeverWidensTheTaskList(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._switch_was = frappe.db.get_value("CRM Tatva Automation", SWITCH, "enabled")
		cls._set_switch(1)
		frappe.get_doc({"doctype": "CRM Task", "title": "ZZ Injection Probe", "status": "Todo"}).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		cls._set_switch(cls._switch_was)
		super().tearDownClass()

	@staticmethod
	def _set_switch(enabled):
		row = frappe.get_doc("CRM Tatva Automation", SWITCH)
		row.enabled = enabled
		row.save(ignore_permissions=True)

	def test_the_clause_runs_and_selects_nothing(self):
		self.assertGreater(frappe.db.count("CRM Task"), 0)
		for user in INJECTED:
			with self.subTest(user=user):
				clause = visibility.scoped_pqc("CRM Task", user)
				self.assertTrue(clause, "a non-privileged user must get a scoping clause")
				rows = frappe.db.sql(f"select count(*) from `tabCRM Task` where {clause}")  # sqli-ok: the clause under test is the input
				self.assertEqual(rows[0][0], 0, f"{user!r} widened the task list")
