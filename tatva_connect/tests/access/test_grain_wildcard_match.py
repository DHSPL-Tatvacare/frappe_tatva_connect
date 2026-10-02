# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A BLANK axis on a contract is a WILDCARD, never the empty string; lead data always carries all three axes.
Locks the contract matcher to the same rule as every other grain matcher in the app."""
from frappe.tests import UnitTestCase

from tatva_connect.taxonomy.grain import AXES, covers, resolve_scoped


def _covers(contract_grain, axes):
	"""The contract question, asked of the one shared matcher in taxonomy.grain."""
	return covers(dict(zip(AXES, contract_grain, strict=True)), *axes)


class TestContractCoversRule(UnitTestCase):
	"""The predicate itself — pure, no DB."""

	def test_blank_axis_is_a_wildcard(self):
		# contract (V, G, blank) covers a lead on ANY program of that vertical+group
		self.assertTrue(_covers(("V", "G", ""), ("V", "G", "P1")))
		self.assertTrue(_covers(("V", "G", ""), ("V", "G", "P2")))
		self.assertTrue(_covers(("V", "", ""), ("V", "G", "P1")))
		self.assertTrue(_covers(("", "", ""), ("V", "G", "P1")))

	def test_set_axis_must_equal(self):
		self.assertTrue(_covers(("V", "G", "P1"), ("V", "G", "P1")))
		self.assertFalse(_covers(("V", "G", "P1"), ("V", "G", "P2")))
		self.assertFalse(_covers(("V2", "G", ""), ("V", "G", "P1")))

	def test_two_casings_are_one_key(self):
		# MariaDB stores grain axes under a ci collation; the matcher must agree with the database.
		self.assertTrue(_covers(("goodflip", "G", ""), ("GOODFLIP", "G", "P1")))

	def test_agrees_with_the_canonical_brain(self):
		"""taxonomy.grain is the reference implementation — the contract matcher must not diverge."""
		axes = ("V", "G", "P1")
		for cg in [("V", "G", "P1"), ("V", "G", ""), ("V", "", ""), ("", "", ""),
		           ("V", "G", "P2"), ("V2", "G", "P1")]:
			canonical = resolve_scoped(
				[{"vertical": cg[0], "group": cg[1], "program": cg[2]}], *axes
			) is not None
			self.assertEqual(
				_covers(cg, axes), canonical,
				f"contract {cg} vs {axes}: diverged from taxonomy.grain",
			)
