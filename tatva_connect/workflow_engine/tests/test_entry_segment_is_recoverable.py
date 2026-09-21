# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A refused write at the FIRST segment must not cost the subject its journey.

Every segment but the first rolls back to the Parked row behind it, and the drain re-drives that. The
entry segment had nothing behind it: `open_journey` and `advance` shared one transaction, so a refused
write — 1020 under `innodb_snapshot_isolation`, which the pool draw's locking read provokes — discarded
the Journey along with the work, and `_bump_retry` found no row to retry. The lead silently never entered
the flow. Observed on prod at 1 in 3 leads on a weighted-distribution graph.

So the Journey commits BEFORE its first segment, and a segment with no suspend behind it parks one drain
interval out rather than sitting Running where `due_journeys` would never look at it again.

Asserted on the MECHANISM, for the reason `test_entry_isolation` records: `FrappeTestCase` wraps each test
in its own transaction, so a commit in here is not the commit a worker makes and an end-to-end assertion
would pass against the broken code and prove nothing.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.workflow_engine.tests.test_entry_segment_is_recoverable
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import interpreter, thresholds, triggers
from tatva_connect.workflow_engine.tests import fixtures as fx

_WORKFLOW = "ZZ Entry Segment Recoverable"


class TestTheEntrySegmentIsRecoverable(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		fx.purge(_WORKFLOW)
		cls.addClassCleanup(fx.purge, _WORKFLOW)
		fx.arm_engine(True, cls=cls)
		cls.workflow = fx.make_workflow(_WORKFLOW, [
			fx.trigger(to="w1"),
			fx.node("w1", "Wait", config={"mode": "For Duration", "duration": "{'minutes': 5}"},
			        edges={"next": "end"}),
			fx.node("end", "Terminal"),
		])
		cls.lead = fx.make_lead()
		cls.addClassCleanup(frappe.delete_doc, "CRM Lead", cls.lead.name, force=True)
		frappe.db.commit()

	def test_the_journey_is_committed_before_its_first_segment(self):
		"""THE fix. A Journey that is not durable when `advance` is refused cannot be retried by anyone."""
		order = []
		journey = frappe._dict(name="zz-not-a-real-journey")

		with patch.object(triggers.interpreter, "open_journey",
		                  side_effect=lambda *a, **k: order.append("open") or journey), \
		     patch.object(triggers.frappe.db, "commit", side_effect=lambda: order.append("commit")), \
		     patch.object(triggers.interpreter, "advance", side_effect=lambda *a, **k: order.append("advance")):
			triggers._start_one(_WORKFLOW, "v1", self.lead.name, {}, None)

		self.assertEqual(
			order, ["open", "commit", "advance"],
			"the Journey must be durable before its first segment runs, or a refused write discards it",
		)

	def test_a_segment_with_no_suspend_behind_it_parks_for_the_drain(self):
		"""The other half: durable is not enough — `due_journeys` reads Parked and nothing else."""
		journey = fx.start_journey(self.workflow, self.lead.name, "w1")
		self.assertEqual(journey.status, "Running", "this test needs a journey the drain would not find")

		interpreter._bump_retry(journey)

		row = frappe.db.get_value(
			fx.JOURNEY_DT, journey.name, ["status", "resume_at", "retry_count"], as_dict=True,
		)
		self.assertEqual(row.status, "Parked", "a Running journey is invisible to the drain forever")
		self.assertIsNotNone(row.resume_at, "a park with no clock never wakes")
		self.assertEqual(row.retry_count, 1)
		self.assertLessEqual(
			frappe.utils.time_diff_in_seconds(row.resume_at, frappe.utils.now_datetime()),
			thresholds.DRAIN_INTERVAL_SECONDS + 5,
			"the retry is paced by the drain's own interval, not left to the backstop",
		)

	def test_a_mid_flow_retry_keeps_the_suspend_it_resumed_from(self):
		"""The regression guard: every other segment already had a durable Parked row, and this must not move it."""
		journey = fx.start_journey(self.workflow, self.lead.name, "w1")
		deadline = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=30)
		frappe.db.set_value(fx.JOURNEY_DT, journey.name,
		                    {"status": "Parked", "resume_at": deadline}, update_modified=False)
		frappe.db.commit()

		interpreter._bump_retry(frappe.get_doc(fx.JOURNEY_DT, journey.name))

		row = frappe.db.get_value(fx.JOURNEY_DT, journey.name, ["status", "resume_at"], as_dict=True)
		self.assertEqual(row.status, "Parked")
		self.assertEqual(
			frappe.utils.get_datetime(row.resume_at), deadline,
			"a mid-flow retry must resume on the clock it already had, never be pulled forward",
		)
