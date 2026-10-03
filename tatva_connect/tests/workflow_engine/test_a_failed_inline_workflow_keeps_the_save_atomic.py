# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A failed inline workflow leaves the triggering save to its request: it rolls back with it, never commits under it."""

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.tests.workflow_engine.fixtures import set_switch
from tatva_connect.workflow_engine import ENGINE_SWITCH, triggers, versions
from tatva_connect.workflow_engine.tests import fixtures as fx


class TestAFailedInlineWorkflowKeepsTheSaveAtomic(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# Registered before super(): it runs after the class rollback, which fires no hook to drop the cached switch row.
		cls.addClassCleanup(frappe.clear_document_cache, "CRM Tatva Automation")
		super().setUpClass()
		# Off, so the lead insert starts no other workflow; the failing one is run through the inline seam itself.
		set_switch(ENGINE_SWITCH, 0)

	def test_a_failed_inline_run_does_not_commit_the_save_it_runs_inside(self):
		"""The run records its failure, and the request's rollback takes the lead and that record together."""
		lead = fx.make_lead()
		workflow = fx.make_workflow(f"ZZ-ATOMIC-{frappe.generate_hash(length=6)}", [fx.trigger(to="ghost")], lifecycle_state="Draft")
		triggers._run_ephemeral(versions.ensure_version(workflow), lead.name, lead, {})
		self.assertEqual(frappe.db.get_value("CRM Workflow Journey", {"subject_name": lead.name}, "status"), "Failed")

		frappe.db.rollback()
		self.assertFalse(frappe.db.exists("CRM Lead", lead.name), "the failed run committed the save it ran inside")
