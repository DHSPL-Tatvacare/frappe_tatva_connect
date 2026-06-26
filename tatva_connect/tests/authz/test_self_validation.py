# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Self-validation — proving the suite isn't blind (TESTS.md §9, the confusion matrix).

A green authz run proves nothing on its own: green can mean "safe" OR "the tests are asleep". This
test validates the validator. For EVERY attack vector it plants a deliberate violation (a negative
control from mutation.py), runs the matching detector, and scores the prediction into a Confusion.
The build FAILS unless:

  * recall == 1.0 on the mutation set      — every planted violation was caught (no False Negative)
  * every ATTACK key has >= 1 mutation     — no vector is silently excluded (audit M2)
  * every ATTACK key has >= 1 case         — registry.cases.attacks_without_cases() is empty
  * every ATTACK key has a TESTABLE mutation OR an explicit untestable-without-code-mutation reason
    — a vector can NEVER reach recall==1.0 by quietly having no detectable plant

Each plant runs in its OWN named savepoint and is rolled back immediately (base.py mutation
discipline — never a bare rollback, which would unwind the class seed floor). Mirrors the self-check
idiom in test_no_perm_bypass.test_scanner_flags_a_planted_bypass: plant a known bug, assert it's
caught.
"""
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
		# The plants reference seeded leads/tasks + the roster (grain users, partner). Fails loud if
		# masters aren't seeded — the accepted precondition (constitution: no auto-seeded masters).
		generator.seed(commit=False)

	# ---- the core: plant every mutation, score it, require recall == 1.0 -------------------------

	def test_planted_violations_are_all_detected(self):
		conf = Confusion()
		false_negatives = []
		for m in mutation.testable():
			cell = self._run_one(m, conf)
			if cell == FN:
				false_negatives.append("{0} [{1}] — {2} (expected detector: {3})".format(
					m["id"], m["attack"], m["english"], m["expected_detector"]))

		# Surface the untestable-without-code-mutation vectors as build-visible warnings — never
		# silently skipped (audit M2: recall must not be gamed by excluding hard vectors).
		warnings = [
			"  {0} [{1}]: {2}".format(u["id"], u["attack"], u["untestable_without_code_mutation"])
			for u in mutation.untestable()
		]
		if warnings:
			frappe.logger().warning(
				"authz self-validation — vectors with NO in-process data plant (need a code-level "
				"mutation / Playwright control):\n" + "\n".join(warnings))

		# The build-failing assertions.
		self.assertFalse(
			false_negatives,
			"FALSE NEGATIVE(S) — the suite stayed GREEN on a planted violation (it is BLIND here):\n"
			+ "\n".join(false_negatives) + "\n" + conf.summary())
		self.assertEqual(
			conf.recall, 1.0,
			"recall < 1.0 on the mutation set — {0}. A planted bug went undetected; "
			"fix the detector before trusting this suite for VAPT.".format(conf.summary()))
		# A real run must have positives (else recall==1.0 is vacuous).
		self.assertGreater(conf.positives, 0, "no positives scored — mutation set is empty/broken")

	def _run_one(self, m, conf):
		"""Plant mutation `m` in its own savepoint, run its detector, score, roll back. Returns the
		confusion cell label. A plant/detect that ERRORS is itself a False Negative (the suite could
		not even exercise the bug), recorded as suite_flagged=False — never swallowed."""
		save_point = "authz_mut_{0}".format(m["id"].replace("-", "_"))
		frappe.db.savepoint(save_point)
		try:
			ctx = m["plant"]()
			flagged = bool(m["detect"](ctx))
		except Exception as exc:  # a broken plant/detect is a blind spot, not a pass
			frappe.logger().error("authz mutation {0} raised: {1}".format(m["id"], exc))
			flagged = False
		finally:
			frappe.db.rollback(save_point=save_point)
		# Every mutation IS a known violation (truth_is_violation=True). Score the prediction.
		return conf.record(truth_is_violation=True, suite_flagged=flagged)

	# ---- coverage guards: no vector silently excluded --------------------------------------------

	def test_every_attack_has_at_least_one_mutation(self):
		by_attack = {}
		for m in mutation.MUTATIONS:
			by_attack.setdefault(m["attack"], []).append(m)
		missing = [k for k in ATTACKS if k not in by_attack]
		self.assertFalse(
			missing,
			"attack vector(s) with NO planted mutation — recall==1.0 would be a lie: {0}".format(
				missing))

	def test_every_attack_has_a_testable_or_declared_mutation(self):
		"""Each vector must have a TESTABLE mutation (real plant+detect) OR every one of its mutations
		is explicitly declared untestable-without-code-mutation. A vector with a mutation that is
		neither testable nor declared is a silent gap."""
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
			"(a silent coverage gap): {0}".format(gaps))

	def test_every_attack_has_a_registry_case(self):
		uncovered = cases.attacks_without_cases()
		self.assertFalse(
			uncovered,
			"attack vector(s) with NO registry case in cases.py: {0}".format(uncovered))
