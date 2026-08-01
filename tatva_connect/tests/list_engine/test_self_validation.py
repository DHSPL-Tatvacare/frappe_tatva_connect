# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Does the list-engine suite actually catch bugs, or is it asleep?

A green run means one of two things and the colour cannot tell you which: the code is right, or the tests
are blind. The 2026-07-31 audit answered that question the expensive way — six severe defects, five of
them invisible to a full green suite — and the operator found a seventh on screen the next morning while
34 tests stayed green.

So this scores the suite against ground truth, the way `tests/authz/test_self_validation.py` already does
for the permission surface. For every plant in `mutation.py` a real defect goes into the PRODUCTION code,
the detector module runs, and the result lands in a confusion matrix:

    truth  = a defect really is present (every plant is, by construction)
    predict = the detector module went RED

    recall < 1.0  ->  a planted bug went undetected  ->  the suite is BLIND there  ->  BUILD FAILS

`confusion.py` is IMPORTED from the authz suite, not copied: it is pure scoring with no authz and no
frappe in it, and a second copy would be the exact duplication this codebase spends its time deleting.

WHAT MAKES THIS EVIDENCE RATHER THAN MORE TEST CODE. Three things, and they are the only reasons to
believe a number produced by the same hands that wrote the bugs:

  * The plants are defects this layer HAS REALLY HAD. Nine of the fifteen replay a specific incident from
    the audit or from a screen an operator looked at. They were not chosen to be easy to catch.
  * The plant is applied to production code and restored in a `finally`. Nothing is mocked, and the
    detector module has no idea it is being tested.
  * A plant that cannot be made honestly is DECLARED untestable with its reason and reported as a
    warning — never quietly dropped to keep recall at 1.0.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_self_validation
"""

import io
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.confusion import FN, Confusion
from tatva_connect.tests.list_engine import mutation


def _run_module(dotted):
	"""Run one test module in-process and say whether it went RED. Nothing else about it is inspected —
	the question is only ever "did the suite notice", which is what a confusion matrix scores."""
	suite = unittest.TestLoader().loadTestsFromName(dotted)
	result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
	return result.testsRun, bool(result.failures or result.errors)


class TestSelfValidation(FrappeTestCase):
	"""The suite's own audit. Every assertion here fails the BUILD, not just a case."""

	def test_the_detectors_are_green_before_anything_is_planted(self):
		"""The control. If a detector is already red, every plant scores a false TP and the recall below
		is meaningless — so the baseline is asserted first and separately."""
		red = []
		for dotted in sorted({m["detector"] for m in mutation.testable()}):
			ran, failed = _run_module(dotted)
			if failed or not ran:
				red.append(f"{dotted} (ran={ran}, red={failed})")
		self.assertFalse(
			red,
			"a detector module is red or empty BEFORE any defect is planted — every score below would be "
			f"a false positive: {red}",
		)

	def test_every_planted_defect_is_detected(self):
		"""THE ONE THAT MATTERS. A planted bug that stays green is a False Negative and names itself."""
		conf = Confusion()
		blind = []
		for m in mutation.testable():
			cell = self._plant_and_score(m, conf)
			if cell == FN:
				blind.append(f"  {m['id']} — {m['english']}  (expected detector: {m['detector']})")

		for m in mutation.untestable():
			frappe.logger().warning(
				f"list-engine self-validation — {m['id']} has no honest in-process plant: "
				f"{m['untestable_without_browser']}"
			)

		self.assertFalse(
			blind,
			"FALSE NEGATIVE(S) — the suite stayed GREEN on a planted defect, so it is BLIND here:\n"
			+ "\n".join(blind)
			+ f"\n{conf.summary()}",
		)
		self.assertEqual(
			conf.recall,
			1.0,
			f"recall < 1.0 on the mutation set — {conf.summary()}. A planted bug went undetected; the "
			"suite is not evidence until the detector is fixed.",
		)
		self.assertGreater(conf.positives, 0, "no positives scored — the mutation set is empty or broken")

	def _plant_and_score(self, m, conf):
		"""Put one real defect into the production code, run its detector, restore, score.

		A plant that RAISES is scored as undetected, never swallowed: a harness that cannot even apply the
		defect has proven nothing about whether the suite would catch it."""
		owner, attribute = mutation.resolve(m["target"])
		original = getattr(owner, attribute)
		try:
			setattr(owner, attribute, m["make"]())
			_ran, flagged = _run_module(m["detector"])
		except Exception as broken:
			frappe.logger().error(f"list-engine mutation {m['id']} raised: {broken}")
			flagged = False
		finally:
			setattr(owner, attribute, original)
			frappe.db.rollback()
		return conf.record(truth_is_violation=True, suite_flagged=flagged)

	def test_no_defect_is_silently_excluded(self):
		"""Recall is trivial to fake by dropping the hard plants. Every mutation must therefore be either
		testable or carry a written reason it is not."""
		mute = [m["id"] for m in mutation.MUTATIONS if not m["untestable_without_browser"] and not m["make"]]
		self.assertFalse(mute, f"mutation(s) with no plant and no declared reason: {mute}")
		self.assertGreaterEqual(
			len(mutation.testable()),
			10,
			"fewer than ten real plants — recall would be a number about almost nothing",
		)
		# Every excluded plant must say WHY in prose an operator could read, not just carry a flag.
		thin = [m["id"] for m in mutation.untestable() if len(m["untestable_without_browser"]) < 80]
		self.assertFalse(thin, f"excluded plant(s) with no real reason written down: {thin}")

	def test_every_audit_finding_has_a_plant(self):
		"""The six severe findings of 2026-07-31, and the colour regression found on screen the next day,
		must each be replayed. A finding with no plant can silently return."""
		required = {
			"MUT-janitor-off",  # #1 retired field bricks a saved view
			"MUT-boot-version-frozen",  # #2 live-on-Save never reaches a rep
			"MUT-kanban-fields-lost",  # #4 card badge lost, rep's choice reverted
			"MUT-union-resolves-ids",  # #5a the scale wall
			"MUT-calendar-window-off",  # #5b the calendar drew the whole table
			"MUT-themes-dropped",  # the grey group-by headers, found on screen
		}
		planted = {m["id"] for m in mutation.MUTATIONS}
		self.assertFalse(
			required - planted,
			f"audit finding(s) with no planted defect — they can return unnoticed: {sorted(required - planted)}",
		)

	def test_the_oracle_never_calls_the_code_it_judges(self):
		"""Ground truth computed by the engine is the engine marking its own homework.

		A source check, because the failure it prevents is invisible at runtime. Read through the AST the
		way `tests/static/_lock_helpers.guard_text` does — docstrings and comments NEVER contribute, or a
		module explaining what it must not call would convict itself, which is exactly what happened on the
		first run of this harness."""
		import ast
		import pathlib

		tree = ast.parse((pathlib.Path(__file__).parent / "oracle.py").read_text())
		body = [
			node
			for node in tree.body
			if not (
				isinstance(node, ast.Expr)
				and isinstance(node.value, ast.Constant)
				and isinstance(node.value.value, str)
			)
		]
		for node in ast.walk(ast.Module(body=body, type_ignores=[])):
			if isinstance(node, ast.FunctionDef) and node.body:
				first = node.body[0]
				if (
					isinstance(first, ast.Expr)
					and isinstance(first.value, ast.Constant)
					and isinstance(first.value.value, str)
				):
					node.body = node.body[1:] or [ast.Pass()]
		code = ast.unparse(ast.Module(body=body, type_ignores=[]))

		for forbidden in ("engine.", "derived.predicate", "derived.union", "derived.group", "get_data"):
			with self.subTest(forbidden):
				self.assertNotIn(
					forbidden,
					code,
					f"oracle.py CALLS {forbidden} — its answers are no longer independent of the code it judges",
				)
