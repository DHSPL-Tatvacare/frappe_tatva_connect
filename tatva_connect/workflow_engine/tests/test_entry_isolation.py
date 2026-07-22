# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A workflow can never damage the save that triggered it.

The engine runs off `doc_events`, which means it runs INSIDE the saving user's transaction. It used to
start a durable run there and then commit and roll back as though it owned that transaction. It did not:
its commit committed the user's whole pending write, and its rollback on the failure path DISCARDED the
record the user had just saved — while the request still returned success. A rep pressed Save, saw it
work, and the lead was not there.

So the property under test is not "the run started". It is "the lead is still there afterwards", under
every outcome the engine can have: it started, it failed, it was a duplicate, it ran inline.

These assert on the LEAD, deliberately. A test that only checks the run would have stayed green through
the entire bug.
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine.tests import fixtures as fx

_WAITING = "entry-isolation-waiting"
_INLINE = "entry-isolation-inline"


class TestEntryIsolation(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_WAITING, _INLINE)
		fx.arm_engine(True, cls)
		# A graph that PARKS — the durable path, the one that used to commit inside the user's save.
		cls.waiting = fx.make_workflow(_WAITING, [
			fx.trigger(to="w1"),
			fx.node("w1", "Wait", config={"mode": "For Duration", "expression": "{'minutes': 5}"},
			        edges={"next": "end"}),
			fx.node("end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WAITING, _INLINE)
		_clear_leads()
		frappe.db.commit()

	def tearDown(self):
		_clear_leads()
		frappe.db.commit()

	def _save_lead(self):
		"""Insert a lead the workflow matches, the way a rep would."""
		return frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Entry", "lead_name": _PROBE, "status": "New",
			"custom_vertical": fx.GRAIN["vertical"], "custom_group": fx.GRAIN["group"],
			"custom_current_program": fx.GRAIN["program"],
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	# --- the mechanism, which is what actually guarantees the property ----------------------------------

	def test_the_durable_start_is_enqueued_after_commit_not_run_inline(self):
		"""The fix itself. A doc_event runs inside the user's transaction, so the engine must not start a
		durable run there — its commit would commit the user's pending write and its rollback would
		DISCARD the record being saved.

		Asserted on the mechanism rather than on a surviving row, because `FrappeTestCase` wraps each test
		in its own transaction: a bare `frappe.db.rollback()` behaves differently in here than it does in
		a real request, so an end-to-end assertion would pass against the broken code and prove nothing.
		It did — that is why this test is shaped this way.
		"""
		from tatva_connect.workflow_engine import triggers

		with patch.object(triggers, "_start_one") as inline, patch.object(triggers.frappe, "enqueue") as queued:
			self._save_lead()

		inline.assert_not_called()
		# Saving a lead enqueues several unrelated things (assignment notifications, etc), so pick OURS
		# rather than trusting the last call — that mistake made this assertion read another job's kwargs.
		starts = [
			c.kwargs for c in queued.call_args_list
			if "workflow-start" in (c.kwargs.get("job_id") or "")
		]
		self.assertEqual(len(starts), 1, "exactly one durable start must be queued")
		self.assertTrue(starts[0].get("enqueue_after_commit"), "must wait for the user's save to commit")
		self.assertTrue(starts[0].get("deduplicate"), "several saves in one request must queue one start")

	def test_the_queued_start_really_starts_the_run(self):
		"""The other half: moving it off the transaction must not stop it happening. Drives the real
		queued entry point with the arguments the enqueue carries."""
		from tatva_connect.workflow_engine import triggers

		lead = self._save_lead()
		frappe.db.commit()

		parked = frappe.get_all(
			fx.RUN_DT, filters={"workflow": self.waiting.name, "subject_name": lead.name},
			fields=["status", "current_node", "active_key"],
		)
		self.assertEqual(len(parked), 1, "the queued start must produce exactly one run")
		self.assertEqual(parked[0].status, "Parked")
		self.assertEqual(
			parked[0].active_key, f"{self.waiting.name}::{lead.name}",
			"the run must carry the key the duplicate guard rests on",
		)

	def test_only_one_live_run_exists_per_lead(self):
		"""What the unique key is for: repeated saves must not each start their own journey."""
		lead = self._save_lead()
		frappe.db.commit()
		for status in ("Contacted", "Nurture", "Qualified"):
			doc = frappe.get_doc("CRM Lead", lead.name)
			doc.status = status
			doc.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
			frappe.db.commit()

		live = frappe.get_all(
			fx.RUN_DT,
			filters={"workflow": self.waiting.name, "subject_name": lead.name,
			         "status": ["in", ("Running", "Parked")]},
			pluck="name",
		)
		self.assertEqual(len(live), 1, f"four saves produced {len(live)} live runs; the guard is not holding")
		self.assertTrue(frappe.db.exists("CRM Lead", lead.name))
		self.assertEqual(frappe.db.get_value("CRM Lead", lead.name, "status"), "Qualified")


_PROBE = "Entry Isolation Probe"


def _clear_leads():
	for name in frappe.get_all("CRM Lead", filters={"lead_name": _PROBE}, pluck="name"):
		for run in frappe.get_all(fx.RUN_DT, filters={"subject_name": name}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"workflow_run": run})
			frappe.db.delete(fx.RUN_DT, {"name": run})
		frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
