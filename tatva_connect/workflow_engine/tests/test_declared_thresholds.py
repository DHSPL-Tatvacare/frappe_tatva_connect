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

from frappe.tests.utils import FrappeTestCase

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
