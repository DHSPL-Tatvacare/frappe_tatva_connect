# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Work waiting in a pool with nobody here to take it: a manager of its line hears once, past their own wait, and a rep never does; read off the Notification Log rows frappe stores."""

from unittest import mock

import frappe
from frappe.tests import freeze_time
from frappe.utils import add_to_date, now_datetime

from tatva_connect.automation.actions import POOL_SIGNAL
from tatva_connect.notifications import dispatch, events
from tatva_connect.tests.workflow_engine import fixtures as fx
from tatva_connect.workflow_engine import interpreter, versions
from tatva_connect.workflow_engine.tests.fixtures import make_workflow, trigger


class TestWorkWaitingTellsItsManagersOnce(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.manager, cls.rep = fx.rep("waiting-manager", "Sales Manager"), fx.rep("waiting-rep")
		pool = fx.pool([(cls.manager, 1, None), (cls.rep, 1, None)], require_checkin=1)  # pool membership is this suite's entitlement
		lead = fx.leads(1)[0]
		workflow = make_workflow(f"ZZ-WAIT-{frappe.generate_hash(length=6)}", [trigger()], lifecycle_state="Draft").name
		# The run the engine leaves when its Distribute step finds nobody to take the lead.
		frappe.get_doc({
			"doctype": interpreter.JOURNEY_DT, "workflow": workflow, "workflow_version": versions.current_name(workflow),
			"subject_doctype": "CRM Lead", "subject_name": lead.name, "status": "Parked",
			"awaiting_signal": POOL_SIGNAL, "awaiting_correlation": pool.name,
		}).insert(ignore_permissions=True, ignore_links=True)  # authz-ok: tier-c — test fixture

	def _told(self, user):
		return frappe.get_all("Notification Log", {"for_user": user, "type": dispatch.WORK_WAITING}, pluck="name")

	def test_a_manager_hears_once_past_their_wait_and_a_rep_never(self):
		# The sweep commits per manager; held here so the class rollback still clears what it wrote.
		with mock.patch.object(dispatch, "armed", return_value=True), mock.patch.object(frappe.db, "commit"):
			events._remind_waiting()
			self.assertEqual(self._told(self.manager), [], "told before their 30 minutes")
			with freeze_time(add_to_date(now_datetime(), minutes=31)):
				events._remind_waiting()
				events._remind_waiting()
		self.assertEqual(len(self._told(self.manager)), 1, "a manager hears once per waiting record")
		self.assertEqual(self._told(self.rep), [], "a rep is not a manager")
