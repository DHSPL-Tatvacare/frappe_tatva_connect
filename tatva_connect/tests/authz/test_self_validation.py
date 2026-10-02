# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The authz suite catches every planted violation (recall 1.0), so a green run means safe, not asleep.
Every attack vector must have a mutation, a registry case, and a testable plant or a declared reason."""
import frappe

from tatva_connect.tests.authz import generator, mutation
from tatva_connect.tests.authz.base import AuthzTestCase
from tatva_connect.tests.authz.confusion import FN, Confusion
from tatva_connect.tests.authz.registry import cases
from tatva_connect.tests.authz.registry.attacks import ATTACKS


class TestSelfValidation(AuthzTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # comms-off gate + class rollback floor
		# Plants use seeded leads, tasks and the roster; this fails loud if masters are not seeded.
		generator.seed(commit=False)

	# ---- the core: plant every mutation, score it, require recall == 1.0 -------------------------

	def test_planted_violations_are_all_detected(self):
		conf = Confusion()
		false_negatives = []
		for m in mutation.testable():
			cell = self._run_one(m, conf)
			if cell == FN:
				false_negatives.append("{} [{}] — {} (expected detector: {})".format(
					m["id"], m["attack"], m["english"], m["expected_detector"]))

		# Log vectors with no in-process plant, so recall is never gamed by dropping hard ones.
		warnings = [
			"  {} [{}]: {}".format(u["id"], u["attack"], u["untestable_without_code_mutation"])
			for u in mutation.untestable()
		]
		if warnings:
			frappe.logger().warning(
				"authz self-validation — vectors with NO in-process data plant (need a code-level "
				"mutation / Playwright control):\n" + "\n".join(warnings))

		self.assertFalse(
			false_negatives,
			"FALSE NEGATIVE(S) — the suite stayed GREEN on a planted violation (it is BLIND here):\n"
			+ "\n".join(false_negatives) + "\n" + conf.summary())
		self.assertEqual(
			conf.recall, 1.0,
			f"recall < 1.0 on the mutation set — {conf.summary()}. A planted bug went undetected; "
			"fix the detector before trusting this suite for VAPT.")
		# A real run must have positives (else recall==1.0 is vacuous).
		self.assertGreater(conf.positives, 0, "no positives scored — mutation set is empty/broken")

	def _run_one(self, m, conf):
		"""Plant `m` in its own savepoint, run its detector, score it and roll back.
		A plant or detector that raises scores as a False Negative."""
		save_point = "authz_mut_{}".format(m["id"].replace("-", "_"))
		frappe.db.savepoint(save_point)
		try:
			ctx = m["plant"]()
			flagged = bool(m["detect"](ctx))
		except Exception as exc:  # a broken plant/detect is a blind spot, not a pass
			frappe.logger().error("authz mutation {} raised: {}".format(m["id"], exc))
			flagged = False
		finally:
			frappe.db.rollback(save_point=save_point)
		# Every mutation is a known violation.
		return conf.record(truth_is_violation=True, suite_flagged=flagged)

	# ---- coverage guards: no vector silently excluded --------------------------------------------

	def test_every_attack_has_at_least_one_mutation(self):
		by_attack = {}
		for m in mutation.MUTATIONS:
			by_attack.setdefault(m["attack"], []).append(m)
		missing = [k for k in ATTACKS if k not in by_attack]
		self.assertFalse(
			missing,
			f"attack vector(s) with NO planted mutation — recall==1.0 would be a lie: {missing}")

	def test_every_attack_has_a_testable_or_declared_mutation(self):
		"""Each vector has a testable mutation, or a mutation declared untestable without a code change."""
		testable_attacks = {m["attack"] for m in mutation.testable()}
		gaps = []
		for k in ATTACKS:
			if k in testable_attacks:
				continue
			declared = [m for m in mutation.MUTATIONS
			            if m["attack"] == k and m["untestable_without_code_mutation"]]
			if not declared:
				gaps.append(k)
		self.assertFalse(
			gaps,
			"attack vector(s) with neither a testable plant nor an untestable-reason declaration "
			f"(a silent coverage gap): {gaps}")

	def test_every_attack_has_a_registry_case(self):
		uncovered = cases.attacks_without_cases()
		self.assertFalse(
			uncovered,
			f"attack vector(s) with NO registry case in cases.py: {uncovered}")
