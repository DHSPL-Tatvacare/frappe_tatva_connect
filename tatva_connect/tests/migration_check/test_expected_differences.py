# TEMPORARY — tests for the migration reconciliation tool. Removed with it; see REMOVE-ME.md.
"""A difference the pipeline caused on purpose must be ATTRIBUTED, on every row it can appear on.

The collapse rule keys nothing on the event code (`harness/rules.py`) — an activity written twice by
LeadSquared is collapsed exactly as a call is. The register footnoted `calls` alone, so the same
deliberate act read as an unexplained variance on the activities row, and a report whose pipeline had
done all three on purpose announced "3 differences to review".

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.migration_check.test_expected_differences
"""
import unittest

from tatva_connect.migration_check import constants as C

# Every resource counted on both sides that is made of activity rows, and can therefore be collapsed.
COLLAPSIBLE = ("activities", "calls")


class TestExpectedDifferences(unittest.TestCase):
	def test_every_collapsible_resource_is_attributed(self):
		for resource in COLLAPSIBLE:
			self.assertIn(resource, C.EXPECTED_DIFFERENCES,
						  f"{resource} is built from activity rows, so the collapse can lower its Frappe "
						  f"figure — unfootnoted, that reads as an unexplained difference")

	def test_the_attribution_names_the_cause_and_the_direction(self):
		"""Naming the direction is the reader's only check on the claim: a collapse can only ever make
		the Frappe figure lower, so more rows in Frappe is a real gap and must not wear this note."""
		for resource, note in C.EXPECTED_DIFFERENCES.items():
			self.assertIn("lower", note, f"{resource}: the note must say which way the difference runs")
			self.assertIn("never higher", note, f"{resource}: the note must exclude the other direction")

	def test_a_resource_the_collapse_cannot_touch_is_not_footnoted(self):
		"""The register explains; it does not excuse. A note on notes or files would make a real
		migration gap look accepted."""
		for resource in ("notes", "files", "tasks_open", "tasks_completed"):
			self.assertNotIn(resource, C.EXPECTED_DIFFERENCES,
							 f"{resource} is not built from activity rows — a difference there is real")
