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


_THRESHOLDS_PKG = "tatva_connect.workflow_engine"
_THRESHOLDS_MOD = f"{_THRESHOLDS_PKG}.thresholds"


def _bindings_of_thresholds(tree):
	"""How THIS module named the thresholds module: (aliases bound to it, names imported straight out)."""
	aliases, direct = set(), set()
	for node in ast.walk(tree):
		if isinstance(node, (ast.Import, ast.ImportFrom)):
			bound, imported = _binding_of(node)
			aliases |= bound
			direct |= imported
	return aliases, direct


def _binding_of(node):
	"""One import statement, in the three spellings the language allows for reaching a constant here."""
	if isinstance(node, ast.Import):                       # import <mod> [as x]
		return {a.asname or a.name for a in node.names if a.name == _THRESHOLDS_MOD}, set()
	if node.module == _THRESHOLDS_MOD:                     # from <mod> import NAME
		return set(), {a.name for a in node.names}
	if node.module == _THRESHOLDS_PKG:                     # from <pkg> import thresholds [as x]
		return {a.asname or a.name for a in node.names if a.name == "thresholds"}, set()
	return set(), set()


class TestEveryDeclaredThresholdHasAReader(FrappeTestCase):
	"""A declaration with no reader is a decision nobody is enforcing.

	`SCHEDULE_TO_DRAIN_HANDOVER` sat unread for the whole of W4 while its comment described behaviour the
	engine did not have, and it was found by hand. So was `RUN_RETENTION_DAYS`. Two by hand is the signal
	that the class needs a lock rather than more looking.

	AST on both sides, never a grep: `thresholds.X` inside a comment or a docstring is not a reader, and a
	name assembled at runtime is not one either — which is why `_ALLOWED_UNREAD` is a list of names with
	written reasons instead of a `getattr` escape hatch.
	"""

	# A declared threshold nothing reads yet, each with the file that owns getting it read. Adding a name
	# here is a deliberate act with a paper trail; it is not a way to make this test go quiet.
	_ALLOWED_UNREAD = {
		"RUN_RETENTION_DAYS": "docs/pending/2026-08-02-run-retention-has-no-reaper.md",
	}

	def _declared(self):
		"""Every module-level constant in thresholds.py, whatever its type — the cron is a string."""
		tree = ast.parse((_ENGINE / "thresholds.py").read_text())
		return {
			target.id
			for node in tree.body if isinstance(node, ast.Assign)
			for target in node.targets if isinstance(target, ast.Name)
		}

	def _read_by_production(self):
		"""Every threshold an app module really evaluates. Tests are excluded on purpose: a constant only
		its own test reads is exactly the defect this locks.

		THE ALIAS IS RESOLVED PER MODULE, never assumed to be `thresholds`. Matching that one spelling
		reported `SWEEP_CRON` unread while `hooks.py` was reading it as `workflow_thresholds.SWEEP_CRON` —
		a lock with a narrower idea of a reader than the language has is a lock that accuses working code.
		"""
		app = _ENGINE.parent
		names = set()
		for path in app.rglob("*.py"):
			if "tests" in path.relative_to(app).parts or path.name == "thresholds.py":
				continue
			tree = ast.parse(path.read_text())
			aliases, direct = _bindings_of_thresholds(tree)
			names |= direct
			names |= {
				node.attr for node in ast.walk(tree)
				if isinstance(node, ast.Attribute)
				and isinstance(node.value, ast.Name)
				and node.value.id in aliases
			}
		return names

	def test_every_threshold_is_read_by_production_code(self):
		unread = self._declared() - self._read_by_production() - set(self._ALLOWED_UNREAD)

		self.assertEqual(
			unread, set(),
			f"declared and read by nothing: {sorted(unread)} — give each one a consumer, or name it in "
			"_ALLOWED_UNREAD with the pending file that owns getting it read",
		)

	def test_the_allowed_list_does_not_outlive_its_reason(self):
		"""The other half. A name excused here and then WIRED UP must lose its excuse, or the next unread
		constant inherits a stale exemption and the lock quietly stops covering it."""
		wired = self._read_by_production() & set(self._ALLOWED_UNREAD)

		self.assertEqual(
			wired, set(),
			f"{sorted(wired)} now has a reader and must come out of _ALLOWED_UNREAD",
		)

	def test_a_threshold_losing_its_last_reader_goes_red(self):
		"""The lock's own proof. Take away every reader of a live constant and this must fail — otherwise
		it passes because the app happens to be tidy, not because it is checking anything."""
		live = self._read_by_production()
		self.assertIn("SWEEP_PAGE", live, "the fixture must pick a constant that IS read")

		with patch.object(type(self), "_read_by_production", lambda _self: live - {"SWEEP_PAGE"}):
			with self.assertRaises(AssertionError) as caught:
				self.test_every_threshold_is_read_by_production_code()

		self.assertIn("SWEEP_PAGE", str(caught.exception))


class TestTheHandoverLandsWhereTheSweepCanCarryIt(FrappeTestCase):
	"""The handover's reasoning, kept executable — and it is NOT the one this class used to hold.

	It asserted `SCHEDULE_TO_DRAIN_HANDOVER < MAX_QUEUED_JOBS / 2`, on the belief that a burst past
	frappe's ceiling would throw partway through. It cannot: `schedule_wake` uses the raw RQ
	`queue.enqueue_at` and never enters `frappe.enqueue`, and `_check_queue_size` reads the READY queue,
	not the scheduled registry. Measured at 600 alarms — `q.count=0`, `registry.count=600`, no throw. A
	frappe constant is now irrelevant to this number and asserting against it encoded a false fact.

	What does hold: the handover is a declared operating choice sized at one sweep page, so at the moment
	alarms stop being set, one pass of the sweep can already carry the entire parked population.
	"""

	def test_the_handover_is_no_larger_than_one_sweep_page(self):
		self.assertLessEqual(thresholds.SCHEDULE_TO_DRAIN_HANDOVER, thresholds.SWEEP_PAGE)


class TestTheLaneIsADeployContractNotOneMachinesComposeFile(FrappeTestCase):
	"""6d: the `workflow` lane lived ONLY in `.localdev/compose.yml`, which is git-excluded.

	Deploy anywhere else and every parked journey sets a timer alarm into a queue nothing services — the journeys
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
		"""The bench this journeys on is itself the proof the contract is satisfiable as written."""
		lane = (frappe.conf.get("workers") or {}).get(wakeups.WAKE_QUEUE)
		self.assertTrue(lane, f"this bench has no `{wakeups.WAKE_QUEUE}` lane registered")
		self.assertGreaterEqual(lane.get("timeout") or 0, thresholds.WAKE_JOB_TIMEOUT)
