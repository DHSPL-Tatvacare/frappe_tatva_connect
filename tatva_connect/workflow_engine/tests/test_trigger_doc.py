# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The record that FIRED a journey is not the same thing as the record the journey is about.

A durable journey's subject is always the resolved parent lead — that is what makes effects act on the lead
and what lets the signal detector find the journey. But the thing whose save actually started it may be a
Task, a File, or a WhatsApp Message. The engine used to keep only the subject, then hand that same lead
to every verb AS the trigger doc.

So a Call API set to send the trigger doc sent the lead; the author's choice changed nothing and nothing
said so. An Update Field aimed at the triggering Task raised and failed the journey. A Create Task's
file-review branch could never fire at all. All three read as "the workflow just doesn't work".

The negative half matters too: a journey the lead itself fired, or one started before this was recorded, has
no separate trigger — and the lead is then the correct answer, not a missing one.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import interpreter
from tatva_connect.workflow_engine.tests import fixtures as fx

_WF = "trigger-doc-probe"


class TestTriggerDoc(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_WF)
		cls.lead = fx.make_lead()
		cls.workflow = fx.make_workflow(_WF, [fx.trigger(to="end"), fx.node("end", "Terminal")])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WF)
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _run(self, **values):
		run = fx.start_journey(self.workflow, self.lead.name, "start")
		if values:
			frappe.db.set_value(fx.JOURNEY_DT, run.name, values)
		return frappe.get_doc(fx.JOURNEY_DT, run.name)

	def test_the_trigger_doc_is_the_record_that_fired_the_run(self):
		"""The headline. A journey started by a Task must hand verbs the TASK."""
		task = frappe.get_doc({
			"doctype": "CRM Task", "title": "probe", "reference_doctype": "CRM Lead",
			"reference_docname": self.lead.name,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		try:
			run = self._run(trigger_doctype="CRM Task", trigger_name=task.name)
			found = interpreter._trigger_doc(run)
			self.assertEqual((found.doctype, found.name), ("CRM Task", task.name))
		finally:
			frappe.delete_doc("CRM Task", task.name, force=True, ignore_permissions=True)

	def test_a_run_with_no_recorded_trigger_falls_back_to_the_subject(self):
		"""A lead-fired run, and every journey frozen before this column existed. The lead is the honest
		answer there — returning nothing would break every verb rather than the one that asked."""
		found = interpreter._trigger_doc(self._run())
		self.assertEqual((found.doctype, found.name), ("CRM Lead", self.lead.name))

	def test_a_deleted_trigger_record_does_not_break_the_run(self):
		"""The task can be gone by the time a parked journey resumes. That must degrade to the lead, not
		raise — the rest of the workflow is still meaningful."""
		run = self._run(trigger_doctype="CRM Task", trigger_name="does-not-exist")
		found = interpreter._trigger_doc(run)
		self.assertEqual((found.doctype, found.name), ("CRM Lead", self.lead.name))
