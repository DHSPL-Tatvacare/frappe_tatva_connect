# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A second triggering record while a run waits is ignored, run alongside or restarts the run, as its Trigger says."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import interpreter, registry, triggers
from tatva_connect.workflow_engine.tests import fixtures as fx

_PREFIX = "ZZ Retrigger"
_OPTIONS = (None, registry.RETRIGGER_ALONGSIDE, registry.RETRIGGER_RESTART)


def _task(lead, title):
	return frappe.get_doc({"doctype": "CRM Task", "title": title, "reference_doctype": "CRM Lead",
	                       "reference_docname": lead, "status": "Todo"}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture


class TestANewTriggerWhileARunWaits(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		cls.workflows, cls.tasks = {}, {}
		# Records first, while the engine is off, so making them starts nothing; each workflow fires on a task's save.
		cls.lead = fx.make_lead()
		cls.addClassCleanup(frappe.delete_doc, "CRM Lead", cls.lead.name, force=True)
		for again in _OPTIONS:
			name = f"{_PREFIX} {again or 'Unset'}"
			fx.purge(name)
			cls.addClassCleanup(fx.purge, name)
			# Each workflow answers only its own tasks, so one save drives one option.
			trigger = fx.trigger(to="w1", subject_doctype="CRM Task", event="Updated",
			                     predicate={"type": "rule", "field": "crm_task.title", "operator": "is", "value": name})
			if again:
				trigger["config"]["retrigger"] = again
			cls.workflows[again] = fx.make_workflow(name, [
				trigger,
				fx.node("w1", "Wait", config={"mode": "For Duration", "duration": "{'minutes': 5}"}, edges={"next": "end"}),
				fx.node("end", "Terminal"),
			]).name
			cls.tasks[again] = (_task(cls.lead.name, name), _task(cls.lead.name, name))
		frappe.db.commit()
		fx.arm_engine(True, cls=cls)

	def _runs(self, again, *order):
		for task in (self.tasks[again][i] for i in order):
			task.reload()
			task.description = frappe.generate_hash(length=8)
			task.save(ignore_permissions=True)
		return sorted((row.trigger_name, row.status) for row in frappe.get_all(
			fx.JOURNEY_DT, filters={"workflow": self.workflows[again], "subject_name": self.lead.name},
			fields=["trigger_name", "status"]))

	def test_each_option_on_a_second_record_and_a_re_save(self):
		names = {again: [str(t.name) for t in tasks] for again, tasks in self.tasks.items()}
		first, second = names[None]
		self.assertEqual(self._runs(None, 0, 1, 0), [(first, "Parked")], "unset ignores a new record while a run waits, as before")
		first, second = names[registry.RETRIGGER_ALONGSIDE]
		self.assertEqual(self._runs(registry.RETRIGGER_ALONGSIDE, 0, 1, 0), sorted([(first, "Parked"), (second, "Parked")]))
		first, second = names[registry.RETRIGGER_RESTART]
		self.assertEqual(self._runs(registry.RETRIGGER_RESTART, 0, 1, 1), sorted([(first, interpreter.STOPPED), (second, "Parked")]))

	def test_a_start_job_is_one_per_record_only_when_the_trigger_acts_on_each(self):
		def job_id(again, record):
			with patch.object(frappe, "enqueue") as enqueue:
				triggers._enqueue_start("W", "V", "L", {}, ("CRM Task", record), {"retrigger": again} if again else {})
			return enqueue.call_args.kwargs["job_id"]

		self.assertEqual(job_id(None, "A"), job_id(None, "B"))
		for again in _OPTIONS[1:]:
			self.assertNotEqual(job_id(again, "A"), job_id(again, "B"), f"{again} drops a second record's start")
			self.assertEqual(job_id(again, "A"), job_id(again, "A"))
