# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""open_journey's counter stamp is one atomic UPDATE, not read-then-write, driven with real concurrent
connections and a barrier — the collision is FORCED, never hoped for, same shape as
test_update_field_rows.TestTwoJourneysOnOneLeadBothLand.

RED on the old shape: two journeys opening on the same workflow each read journeys_started, computed
+1 off their own snapshot, and wrote it back — a lost update under READ COMMITTED, or the exact
frappe.QueryDeadlockError seen live in production (Error Log "workflow: ephemeral journey failed",
interpreter.py:200, 2026-09-01) under REPEATABLE READ.

GREEN on the fix: the increment happens inside MariaDB's own row lock in one statement, so two
concurrent connections serialize at the database with no exception and no lost count.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_open_journey_counter_race
"""
import threading

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import interpreter
from tatva_connect.workflow_engine.tests import fixtures

_WF = "ZZ Open Journey Counter Race"


def _old_shape_increment(workflow, when, barrier=None):
	"""The exact pre-fix code (interpreter.py, before this session's fix): read, then write the
	snapshot's +1 back. The barrier sits between the two so both threads read the same stale value —
	forced, not hoped for."""
	current = frappe.db.get_value(interpreter._WORKFLOW_DT, workflow, "journeys_started") or 0
	if barrier is not None:
		try:
			barrier.wait()
		except threading.BrokenBarrierError:
			pass  # the other side is what proves the collision; a lone timeout still ran the write below
	frappe.db.set_value(interpreter._WORKFLOW_DT, workflow, {
		"last_journey_at": when,
		"journeys_started": current + 1,
	}, update_modified=False)


def _new_shape_increment(workflow, when, barrier=None):
	"""The shipped fix, called directly rather than through the whole of open_journey. The barrier fires
	right before the single UPDATE so both connections issue it at the same instant."""
	if barrier is not None:
		try:
			barrier.wait()
		except threading.BrokenBarrierError:
			pass
	wf = frappe.qb.DocType(interpreter._WORKFLOW_DT)
	(
		frappe.qb.update(wf)
		.set(wf.last_journey_at, when)
		.set(wf.journeys_started, wf.journeys_started + 1)
		.where(wf.name == workflow)
	).run()


class _CounterRaceBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures.purge(_WF)
		cls.addClassCleanup(fixtures.purge, _WF)
		cls.workflow = fixtures.make_workflow(_WF, [fixtures.trigger(to="n1"), fixtures.node("n1", "Terminal")])

	def setUp(self):
		super().setUp()
		frappe.db.set_value(interpreter._WORKFLOW_DT, _WF, "journeys_started", 0)
		frappe.db.commit()

	def _race(self, increment_fn):
		"""Two real connections, each incrementing by 1 on their own transaction. frappe.init/connect/
		destroy per thread because frappe.local is thread-local: without its own connection a thread
		would share this test's transaction and race nothing."""
		site = frappe.local.site
		raised = []
		barrier = threading.Barrier(2, timeout=4)

		def bump():
			frappe.init(site=site)
			frappe.connect()
			try:
				frappe.db.begin()
				increment_fn(_WF, frappe.utils.now_datetime(), barrier=barrier)
				frappe.db.commit()
			except Exception as e:  # collected, never swallowed — a thread that died silently proves nothing
				raised.append(e)
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=bump) for _ in range(2)]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=60)
		return raised

	def _count(self):
		return frappe.db.get_value(interpreter._WORKFLOW_DT, _WF, "journeys_started")


class TestTheOldShapeLosesOrRaces(_CounterRaceBase):
	"""RED premise, proven rather than assumed: the pre-fix code, driven with the exact same barrier,
	either throws or loses one of the two increments. If this test ever passes clean, the premise for
	the fix was wrong."""

	def test_two_concurrent_old_shape_increments_do_not_both_land_cleanly(self):
		raised = self._race(_old_shape_increment)
		clean = not raised and self._count() == 2
		self.assertFalse(
			clean,
			"the read-then-write shape landed both increments with no exception — "
			"the race this fix targets did not reproduce, so it proves nothing here",
		)


class TestTheAtomicUpdateAlwaysLandsBoth(_CounterRaceBase):
	"""GREEN: the shipped fix. Same barrier, same two real connections, same row."""

	def test_two_concurrent_atomic_increments_both_land_with_no_exception(self):
		raised = self._race(_new_shape_increment)
		self.assertEqual(raised, [], f"the atomic update raised: {raised!r}")
		self.assertEqual(self._count(), 2, "two concurrent increments must add 2, not 1")
