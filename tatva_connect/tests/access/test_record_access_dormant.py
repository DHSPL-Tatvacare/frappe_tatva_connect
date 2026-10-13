# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A dormant index costs nothing: with `Access::Record::acl` off, saving and assigning a lead write no grant row.

Before 2026-10-13 the writers ran whatever the switch said, so prod kept 74,663 unread rows current on every assignment.
"""
import frappe
from frappe.desk.form import assign_to
from frappe.tests import IntegrationTestCase

from tatva_connect.access import record_access
from tatva_connect.workflow_engine.tests import fixtures as fx


class TestRecordAccessDormant(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.was = frappe.db.get_value("CRM Tatva Automation", record_access.SWITCH, "enabled")
		self.lead = None

	def tearDown(self):
		self._switch(self.was)
		if self.lead:
			frappe.db.delete("ToDo", {"reference_type": "CRM Lead", "reference_name": self.lead.name})
			frappe.db.delete(record_access.DOCTYPE, {"reference_name": self.lead.name})
			frappe.delete_doc("CRM Lead", self.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def _switch(self, enabled):
		row = frappe.get_doc("CRM Tatva Automation", record_access.SWITCH)
		row.enabled = enabled
		row.save(ignore_permissions=True)

	def _grants(self):
		return frappe.db.count(record_access.DOCTYPE, {"reference_name": self.lead.name})

	def test_off_writes_nothing_and_on_writes_the_grant(self):
		owner = record_access.SUBJECTS["CRM Lead"]
		self._switch(0)
		self.lead = fx.make_lead(**{owner: None})
		assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": ["Administrator"]})
		self.assertEqual(self._grants(), 0, "a dormant index was written")

		self._switch(1)
		other = frappe.db.get_value("User", {"enabled": 1, "user_type": "System User", "name": ("not in", ("Administrator", "Guest"))})
		self.lead.reload()
		self.lead.set(owner, other)  # a real change: the assignment above already stamped Administrator as owner
		self.lead.save(ignore_permissions=True)
		self.assertGreater(self._grants(), 0, "an armed index was not written")
