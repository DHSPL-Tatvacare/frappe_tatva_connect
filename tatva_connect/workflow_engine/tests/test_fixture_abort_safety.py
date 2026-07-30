# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""AN ABORTED setUpClass MUST NOT LEAVE THE ENGINE ARMED.

`fx.arm_engine` used to hand back the previous value for the caller to restore in `tearDownClass`.
`unittest` skips `tearDownClass` entirely when `setUpClass` raises, so an abort left the switch ON — and
the leak was self-propagating, which is what made it dangerous rather than untidy:

  1. a setUpClass armed the engine and then raised (a bad fixture, a duplicate key);
  2. tearDownClass never ran, so the switch stayed ON;
  3. every later suite read `was = 1`, recorded it as "the original value", and faithfully restored the
     engine to ON — while reporting a clean teardown.

So the bench drifted to armed with nothing anywhere going red, and "no switches left on" stopped being
something a test could falsify. It was caught by hand, twice, by reading `modified` timestamps.

The fix is `addClassCleanup`, which `unittest` runs even when `setUpClass` raises. This suite proves it by
BUILDING a suite that aborts and running it, rather than by asserting that the fix is present — a test
that checked for the call would pass against a call that did nothing.

Nothing here sends. It only writes the engine switch, and it asserts that switch is back OFF at the end.
"""
import io
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import automation
from tatva_connect.workflow_engine import ENGINE_SWITCH
from tatva_connect.workflow_engine.tests import fixtures as fx


def _engine_is_on() -> bool:
	return bool(automation.is_enabled(ENGINE_SWITCH))


def _run_isolated(case_cls) -> None:
	"""Run one TestCase class the way the runner would, swallowing its result."""
	suite = unittest.TestLoader().loadTestsFromTestCase(case_cls)
	unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)


class TestArmEngineIsAbortSafe(FrappeTestCase):
	"""The switch must come back OFF whether the suite that armed it finished, failed, or never started."""

	def setUp(self):
		fx._set_engine(False)
		self.assertFalse(_engine_is_on(), "the probe starts from a known-OFF bench")

	def tearDown(self):
		fx._set_engine(False)

	def test_a_suite_that_dies_in_setupclass_still_disarms_the_engine(self):
		"""THE red. This is the exact sequence that left the engine armed during W0.4."""

		class _Aborting(unittest.TestCase):
			@classmethod
			def setUpClass(cls):
				fx.arm_engine(True, cls)
				raise RuntimeError("fixture blew up after arming, exactly as a duplicate key does")

			def test_never_runs(self):
				raise AssertionError("unreachable")

		_run_isolated(_Aborting)

		self.assertFalse(
			_engine_is_on(),
			"an aborted setUpClass left the engine ARMED — every later suite will now read that as the "
			"original value and restore it to ON while reporting a clean teardown",
		)

	def test_a_suite_that_fails_in_a_test_still_disarms_the_engine(self):
		"""The ordinary red-test path, which the old shape did handle — kept so a future change cannot fix
		the abort case by breaking this one."""

		class _Failing(unittest.TestCase):
			@classmethod
			def setUpClass(cls):
				fx.arm_engine(True, cls)

			def test_fails(self):
				raise AssertionError("an ordinary failing test")

		_run_isolated(_Failing)

		self.assertFalse(_engine_is_on())

	def test_a_suite_that_passes_disarms_the_engine_and_it_was_really_armed(self):
		"""Both directions. A cleanup that disarmed unconditionally would pass the two tests above while
		the engine was never armed at all — so this asserts the switch was ON while the test ran."""
		seen = {}

		class _Passing(unittest.TestCase):
			@classmethod
			def setUpClass(cls):
				fx.arm_engine(True, cls)

			def test_sees_the_engine_armed(self):
				seen["armed"] = _engine_is_on()

		_run_isolated(_Passing)

		self.assertTrue(seen.get("armed"), "the engine was never actually armed, so this proves nothing")
		self.assertFalse(_engine_is_on())

	def test_arming_a_bench_that_is_already_armed_fails_loudly(self):
		"""The half that `addClassCleanup` alone does not fix, and it was found the hard way: after the
		cleanup landed, the switch was STILL ON at the end of a journey, because the baseline had already been
		poisoned and every suite was faithfully restoring `was = 1`.

		Restoring "whatever it was" is the propagation mechanism. OFF is the only correct resting state of
		a dormant-by-default bench, so a suite that finds the engine already armed has found a leak and
		says so rather than inheriting it.
		"""
		fx._set_engine(True)

		class _Nested(unittest.TestCase):
			@classmethod
			def setUpClass(cls):
				fx.arm_engine(True, cls)

			def test_never_runs(self):
				raise AssertionError("unreachable")

		with self.assertRaises(AssertionError) as caught:
			_Nested.setUpClass()

		self.assertIn("already ON", str(caught.exception))

	def test_the_engine_is_left_off_even_when_it_started_on(self):
		"""And the leak is CLEANED, not merely reported: a journey that starts poisoned must not end poisoned."""
		fx._set_engine(True)

		class _Nested(unittest.TestCase):
			@classmethod
			def setUpClass(cls):
				try:
					fx.arm_engine(True, cls)
				except AssertionError:
					fx.arm_engine(True)  # the in-test form, which does not police the baseline
					cls.addClassCleanup(fx._set_engine, False)

			def test_noop(self):
				pass

		_run_isolated(_Nested)

		self.assertFalse(_engine_is_on(), "the resting state of this bench is OFF, whatever it started as")
