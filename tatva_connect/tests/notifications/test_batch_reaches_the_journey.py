# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A journey a bulk action starts keeps that bulk action: walked later by the worker, its Assign joins the action's wave instead of alerting per lead; a journey a rep's own save starts keeps none."""
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.lead import assignment
from tatva_connect.notifications import batch
from tatva_connect.workflow_engine import interpreter, versions
from tatva_connect.workflow_engine.tests import fixtures as fx

ACTION = "CRM List Action Job"
REP = "zz-batch-journey-rep@example.invalid"


class TestBatchReachesTheJourney(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.make_user(REP)
		# An assignee must be entitled to the lead's grain, as the engine refuses anyone who may not see it.
		cls.entitled = frappe.get_doc({"doctype": "User Permission", "user": REP, "allow": "CRM Vertical", "for_value": fx.GRAIN["vertical"],
		                               "applicable_for": "CRM Lead", "apply_to_all_doctypes": 0}).insert(ignore_permissions=True).name
		cls.workflow = fx.make_workflow("ZZ batch reaches the journey", [
			fx.trigger(to="n1", event="Updated"),
			fx.node("n1", "Assign to User", {"assignee_mode": "User", "assign_to_user": REP, "assign_mode": "Assign"}, edges={"assigned": "end"}),
			fx.node("end", "Terminal"),
		], lifecycle_state="Published")
		frappe.db.commit()
		cls.addClassCleanup(fx.purge, cls.workflow.name)
		cls.addClassCleanup(frappe.delete_doc, "User Permission", cls.entitled, force=True, ignore_permissions=True)

	def setUp(self):
		with patch("tatva_connect.tatva_connect.doctype.crm_list_action_job.crm_list_action_job.enqueue"):  # the record only, never a queued run a worker would try
			self.job = frappe.get_doc({"doctype": ACTION, "action": "Bulk Edit", "target_doctype": "CRM Lead"}).insert(ignore_permissions=True).name
		self.lead = fx.make_lead().name
		self.addCleanup(self._remove)

	def _remove(self):
		frappe.db.rollback()  # a fresh read first: the walk committed, so a delete from the old snapshot is refused (1020)
		for doctype, name in (("CRM Lead", self.lead), (ACTION, self.job)):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _open(self):
		journey = interpreter.open_journey(self.workflow.name, versions.current_name(self.workflow.name), self.lead, {})
		frappe.db.commit()  # a walk commits or rolls back on its own, so what it walks must already stand
		return journey

	def test_a_journey_a_bulk_edit_starts_is_told_in_its_wave_when_walked_later(self):
		with batch.holding(ACTION, self.job):
			journey = self._open()
		self.assertEqual((journey.batch_doctype, journey.batch_name), (ACTION, self.job))
		wave = frappe.as_json([ACTION, self.job, "CRM Lead"], indent=None)
		self.addCleanup(batch._run, ("delete", batch._key("tally", wave), batch._key("last", wave)), ("srem", batch._key("open"), wave))
		with patch("frappe.desk.form.assign_to.notify_assignment") as alerted:
			interpreter.advance(journey)  # the worker, later, outside the bulk run
		self.assertEqual(assignment.current_assignees("CRM Lead", self.lead), [REP], fx.logs(journey.name))
		alerted.assert_not_called()
		tally = batch._run(("hgetall", batch._key("tally", wave)))[0]
		self.assertEqual({user.decode(): int(n) for user, n in tally.items()}, {REP: 1}, "the assignment joined the bulk action's wave")
		self.assertEqual(frappe.db.get_value("CRM Lead", self.lead, list(batch.STAMP)), (ACTION, self.job))

	def test_a_journey_a_reps_own_save_starts_keeps_no_bulk_action(self):
		journey = self._open()
		self.assertEqual((journey.batch_doctype, journey.batch_name), (None, None))
