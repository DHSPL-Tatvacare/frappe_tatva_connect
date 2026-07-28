# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W4.3 — the numbers the engine paces itself by are DECLARED, and declared exactly once.

A chunk size or a retention window written inline reads as implementation detail, and the engine ends up
inferring its own behaviour — the defect W4 exists to delete (§11). Collecting them is easy; what keeps
them collected is this file. It fails when a module grows a private numeric knob beside the declaration,
which is how the first copy always appears.

AST, not a grep: a comment mentioning 100 is not a second brain and must not go red.
"""
import ast
import pathlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import automation
from tatva_connect.storage import call_media
from tatva_connect.workflow_engine import drain, thresholds, wakeups

_ENGINE = pathlib.Path(__file__).resolve().parent.parent

# The modules W4.3 collected numbers OUT of. A knob reappearing in one of these is the regression.
_PACED = ("wakeups.py", "drain.py")

# Not knobs: a hop ceiling and a retry count are correctness bounds inside one walk, not pacing an
# operator or a plan ever reasons about, and moving them would make `thresholds` a junk drawer.
_ALLOWED = {"MAX_HOPS", "MAX_RETRIES"}


def _module_level_numbers(path):
	"""Every `NAME = <number>` at module level, by name — the shape a private threshold always takes."""
	tree = ast.parse(path.read_text())
	found = {}
	for node in tree.body:
		if not isinstance(node, ast.Assign):
			continue
		if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, (int, float)):
			continue
		for target in node.targets:
			if isinstance(target, ast.Name):
				found[target.id] = node.value.value
	return found


class TestNoModuleKeepsAPrivateThreshold(FrappeTestCase):
	def test_the_paced_modules_declare_no_numbers_of_their_own(self):
		for filename in _PACED:
			with self.subTest(module=filename):
				found = set(_module_level_numbers(_ENGINE / filename)) - _ALLOWED

				self.assertEqual(
					found, set(),
					f"{filename} declares {sorted(found)} — thresholds are declared in thresholds.py",
				)

	def test_the_modules_really_read_the_declaration(self):
		"""The other half: a module could satisfy the lock above by hardcoding the number inline instead."""
		self.assertIs(wakeups.thresholds, thresholds)
		self.assertIs(drain.thresholds, thresholds)


class TestEveryAccumulatingKindDeclaresTwoAges(FrappeTestCase):
	"""The cleanup posture: everything ends, and its two ages live in ONE place.

	DEAD_AFTER is the short age — how long the thing could legitimately still matter, after which it is
	CLOSED with a reason. RETENTION is the long one — the audit trail outlives the behaviour. A kind with
	only one of them has an unanswerable half, and a literal at a call site is the second brain this locks.
	"""

	def test_each_kind_declares_both_ages(self):
		for kind, dead, retention in (
			("workflow signal", "SIGNAL_DEAD_AFTER_DAYS", "SIGNAL_RETENTION_DAYS"),
			("call media", "MEDIA_DEAD_AFTER_DAYS", "MEDIA_RETENTION_DAYS"),
		):
			with self.subTest(kind=kind):
				self.assertTrue(hasattr(thresholds, dead), f"{kind} declares no dead-age")
				self.assertTrue(hasattr(thresholds, retention), f"{kind} declares no retention age")

	def test_closing_always_comes_before_deleting(self):
		"""Closing is the SAFETY act and deleting is only housekeeping, so a row can never be deleted
		while it could still act — that is what "dead-age shorter than retention" means in numbers."""
		self.assertLess(thresholds.SIGNAL_DEAD_AFTER_DAYS, thresholds.SIGNAL_RETENTION_DAYS + 30)
		self.assertLess(thresholds.MEDIA_DEAD_AFTER_DAYS, thresholds.MEDIA_RETENTION_DAYS)

	def test_the_media_reaper_really_reads_the_declaration(self):
		"""`call_media` is the second kind to accumulate; it must read the same module, not copy a number."""
		self.assertIs(call_media.thresholds, thresholds)

	def test_the_media_reaper_rides_the_sweep_that_already_exists(self):
		"""One sweep, not one per problem. A second scheduler entry for reaping would be the defect."""
		from tatva_connect import hooks

		scheduled = [path for bucket in hooks.scheduler_events["cron"].values() for path in bucket]
		reapers = [path for path in scheduled if "call_media" in path]
		self.assertEqual(
			reapers, ["tatva_connect.storage.call_media.sweep"],
			"the media reaper must ride the existing sweep, never its own scheduler entry",
		)


class TestTheSweepCadenceIsDeclaredOnce(FrappeTestCase):
	"""`hooks.py` is where a cadence silently drifts from the plan that decided it."""

	def test_hooks_runs_the_sweep_on_the_declared_cron(self):
		from tatva_connect import hooks

		cron = hooks.scheduler_events["cron"]

		self.assertIn(thresholds.SWEEP_CRON, cron, "no cron bucket matches the declared sweep cadence")
		self.assertIn("tatva_connect.workflow_engine.wakeups.sweep", cron[thresholds.SWEEP_CRON])
		self.assertIn("tatva_connect.workflow_engine.drain.sweep", cron[thresholds.SWEEP_CRON])


class TestTheHandoverIsUnderFrappesOwnCeiling(FrappeTestCase):
	"""§10.2: a threshold at or near MAX_QUEUED_JOBS fails by erroring partway through the very burst it
	was meant to survive. This is the reasoning, kept executable."""

	def test_it_leaves_headroom_below_max_queued_jobs(self):
		from frappe.utils.background_jobs import MAX_QUEUED_JOBS

		self.assertLess(thresholds.SCHEDULE_TO_DRAIN_HANDOVER, MAX_QUEUED_JOBS / 2)


class TestTheLaneIsADeployContractNotOneMachinesComposeFile(FrappeTestCase):
	"""6d: the `workflow` lane lived ONLY in `.localdev/compose.yml`, which is git-excluded.

	Deploy anywhere else and every parked run sets a timer alarm into a queue nothing services — the runs
	park correctly, the alarms are written correctly, and not one of them ever fires. There is no error
	and no red anywhere, which is exactly why this assert exists.
	"""

	def test_an_armed_engine_without_the_lane_fails_the_migrate(self):
		"""Loud beats silent. The message must name the lane and what to run, or it is not actionable."""
		with patch.object(automation, "is_enabled", return_value=True), \
		     patch.dict(frappe.conf, {"workers": {}}, clear=False):
			with self.assertRaises(frappe.ValidationError) as caught:
				wakeups.assert_lane_registered()

		self.assertIn(wakeups.WAKE_QUEUE, str(caught.exception))
		self.assertIn("bench worker", str(caught.exception))

	def test_a_lane_whose_timeout_is_below_the_declared_one_fails_too(self):
		"""A lane that exists but is capped under `WAKE_JOB_TIMEOUT` kills a long segment mid-flight."""
		short = {wakeups.WAKE_QUEUE: {"background_workers": 1, "timeout": thresholds.WAKE_JOB_TIMEOUT - 1}}
		with patch.object(automation, "is_enabled", return_value=True), \
		     patch.dict(frappe.conf, {"workers": short}, clear=False):
			with self.assertRaises(frappe.ValidationError):
				wakeups.assert_lane_registered()

	def test_a_dormant_engine_needs_no_lane(self):
		"""The engine ships OFF, so a fresh install has no lane and is correct — and `install-app` cannot
		set `workers` before the app that needs it exists, so a throw there would fail the install."""
		with patch.object(automation, "is_enabled", return_value=False), \
		     patch.dict(frappe.conf, {"workers": {}}, clear=False):
			wakeups.assert_lane_registered()  # must not raise

	def test_this_bench_registers_the_lane_it_declares(self):
		"""The bench this runs on is itself the proof the contract is satisfiable as written."""
		lane = (frappe.conf.get("workers") or {}).get(wakeups.WAKE_QUEUE)
		self.assertTrue(lane, f"this bench has no `{wakeups.WAKE_QUEUE}` lane registered")
		self.assertGreaterEqual(lane.get("timeout") or 0, thresholds.WAKE_JOB_TIMEOUT)
