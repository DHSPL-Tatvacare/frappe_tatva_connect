# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A scheduled cohort is a bulk action: one drain pass books its leads and walks them, and their assignments join the cohort's one wave, never alerting per lead."""
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.lead import assignment
from tatva_connect.notifications import batch
from tatva_connect.workflow_engine import drain, registry
from tatva_connect.workflow_engine.tests import fixtures as fx

REP = "zz-cohort-wave-rep@example.invalid"
MARKER = "zz-cohort-is-a-bulk-action"


class TestCohortIsABulkAction(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.workflow_name = "ZZ Cohort Is A Bulk Action"
		fx.purge(cls.workflow_name)
		cls.addClassCleanup(fx.purge, cls.workflow_name)
		fx.arm_engine(True, cls=cls)
		fx.make_user(REP)
		# An assignee must be entitled to the lead's grain, as the engine refuses anyone who may not see it.
		cls.entitled = frappe.get_doc({"doctype": "User Permission", "user": REP, "allow": "CRM Vertical", "for_value": fx.GRAIN["vertical"],
		                               "applicable_for": "CRM Lead", "apply_to_all_doctypes": 0}).insert(ignore_permissions=True).name
		cls.leads = [fx.make_lead(custom_external_id=MARKER).name for _ in range(3)]
		cls.addClassCleanup(cls._drop)
		fx.make_workflow(cls.workflow_name, [
			fx.node("start", "Trigger", config={
				"mode": registry.MODE_SCHEDULE, "subject_doctype": "CRM Lead", "schedule": "Daily", "schedule_time": "09:00",
				"vertical": fx.GRAIN["vertical"], "group": fx.GRAIN["group"], "program": fx.GRAIN["program"],
				"predicate": {"type": "rule", "field": "crm_lead.custom_external_id", "operator": "is", "value": MARKER},
			}, edges={"next": "n1"}),
			fx.node("n1", "Assign to User", {"assignee_mode": "User", "assign_to_user": REP, "assign_mode": "Assign"}, edges={"assigned": "end"}),
			fx.node("end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def _drop(cls):
		frappe.db.rollback()
		for lead in cls.leads:
			frappe.delete_doc("CRM Lead", lead, force=True, ignore_permissions=True)
		frappe.delete_doc("User Permission", cls.entitled, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		fx.forget_pass()
		self.addCleanup(fx.forget_pass)  # the pass books its successor on the lane; none is left behind

	def test_one_pass_books_and_walks_a_cohort_in_one_wave(self):
		frappe.db.set_value(fx.WORKFLOW_DT, self.workflow_name, "trigger_next_run_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1),
		                    update_modified=False)  # the schedule is due now
		frappe.db.commit()
		wave = frappe.as_json([batch.SCHEDULED, self.workflow_name, "CRM Lead"], indent=None)
		self.addCleanup(batch._run, ("delete", batch._key("tally", wave), batch._key("last", wave)), ("srem", batch._key("open"), wave))
		with patch("frappe.desk.form.assign_to.notify_assignment") as alerted:
			drain.run()  # ONE pass: before, it only booked the leads and the next pass walked them
		self.assertEqual({lead: assignment.current_assignees("CRM Lead", lead) for lead in self.leads}, {lead: [REP] for lead in self.leads})
		alerted.assert_not_called()
		tally = batch._run(("hgetall", batch._key("tally", wave)))[0]
		self.assertEqual({user.decode(): int(n) for user, n in tally.items()}, {REP: len(self.leads)}, "the cohort's one wave holds every lead it gave")
