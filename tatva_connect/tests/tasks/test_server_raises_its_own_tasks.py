# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A task the SERVER raises does not depend on who tripped it.

`create_followup_task` is a documented HTTP endpoint, so it asks the caller to prove lead write. The
workflow engine called that same function and inherited a gate meant for a principal on the wire: a
public enrolment form submits as `Guest`, Guest cannot write a lead, and every intake lead that should
have raised a task failed at `create-task-1` instead. The wire path keeps its check; the server's own
callers use `raise_followup_task`, which is neither whitelisted nor gated.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity.api import scope_applies_to_lead
from tatva_connect.tasks.tasks import create_followup_task, raise_followup_task
from tatva_connect.workflow_engine.tests import fixtures as fx


def _a_type_for(lead):
	"""A task type this lead's grain really runs — asked of the brain the helper asks."""
	for name in frappe.get_all("CRM Task Type", pluck="name"):
		if scope_applies_to_lead(name, lead):
			return name
	raise AssertionError("no task type applies to the fixture lead's grain — the fixture is wrong")


class TestTheServerRaisesItsOwnTasks(FrappeTestCase):
	def setUp(self):
		# Per test, not per class: FrappeTestCase rolls back after each one, which would take a
		# class-level lead with it and leave the second test with nothing to act on.
		self.lead = fx.make_lead(first_name="ZZ Guest Probe")
		self.task_type = _a_type_for(self.lead.name)

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_a_guest_triggered_run_still_raises_the_task(self):
		"""The failure from a real submission: a public form submits as Guest."""
		frappe.set_user("Guest")

		task = raise_followup_task(lead=self.lead.name, task_type=self.task_type, title="ZZ from Guest")

		self.assertTrue(frappe.db.exists("CRM Task", task))

	def test_the_wire_endpoint_still_refuses_a_caller_who_cannot_write_the_lead(self):
		"""The other half — nothing a principal can reach is widened by the split."""
		frappe.set_user("Guest")

		with self.assertRaises(frappe.PermissionError):
			create_followup_task(lead=self.lead.name, task_type=self.task_type, title="ZZ over the wire")
