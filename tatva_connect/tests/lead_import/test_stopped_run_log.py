# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A stopped Live run's Import Log names the leads its Stop removed, never calling them Successfully Imported."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.lead_import import api
from tatva_connect.workflow_engine.tests.fixtures import make_lead


class TestStoppedRunLog(IntegrationTestCase):
	def test_only_a_stopped_runs_vanished_leads_read_as_removed(self):
		kept, gone = make_lead(mobile_no="+916100093001").name, make_lead(mobile_no="+916100093002").name
		job = frappe.get_doc({"doctype": "CRM Bulk Job", "operation": "lead_import", "status": "JobComplete"}).insert(ignore_permissions=True)
		frappe.delete_doc("CRM Lead", gone, force=True, ignore_permissions=True)  # as Stop's compensation deletes what the run created
		self.assertEqual(api._removed(job.name, [kept, gone]), set())  # a run that was never stopped still reads as imported
		job.db_set("status", "Aborted")
		self.assertEqual(api._removed(job.name, [kept, gone]), {gone})
