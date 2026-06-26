# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The confusion matrix — proving the suite isn't blind (TESTS.md §9).

A green run proves nothing on its own: green can mean "safe" OR "the tests are asleep". So we
score the suite's own predictions against ground truth and report precision/recall.

    ground truth          = does a violation TRULY exist (the oracle / a known-bad fixture label)
    prediction            = did the suite FLAG it (go red)
    recall < 1.0          = a planted violation slipped through = a False Negative = FAIL THE BUILD

This module is PURE logic — no DB, no frappe. It just accumulates the four matrix cells and derives
the metrics, so it can be unit-tested standalone and fed into report.py / the PDF.

    |                       | violation truly exists | no violation |
    | suite flags it (red)  | TP                     | FP           |
    | suite stays green     | FN  (the dangerous one)| TN           |
"""

# The four matrix cells, as a (truth_is_violation, suite_flagged) -> label map.
TP = "TP"  # violation exists AND suite flagged it          — detection works
FP = "FP"  # no violation BUT suite flagged it               — false alarm, fix the test
FN = "FN"  # violation exists BUT suite stayed green         — BLIND; the dangerous cell
TN = "TN"  # no violation AND suite stayed green             — correct


def classify(truth_is_violation, suite_flagged):
	"""Map a (ground-truth, prediction) pair to its confusion-matrix cell label."""
	if truth_is_violation:
		return TP if suite_flagged else FN
	return FP if suite_flagged else TN


class Confusion:
	"""Accumulates the four cells across many (truth, prediction) observations."""

	def __init__(self):
		self.tp = 0
		self.fp = 0
		self.fn = 0
		self.tn = 0

	def record(self, truth_is_violation, suite_flagged):
		"""Score one observation into its cell. Returns the cell label (for per-case reporting)."""
		cell = classify(bool(truth_is_violation), bool(suite_flagged))
		if cell == TP:
			self.tp += 1
		elif cell == FP:
			self.fp += 1
		elif cell == FN:
			self.fn += 1
		else:
			self.tn += 1
		return cell

	@property
	def positives(self):
		"""Observations where a violation truly exists (TP + FN) — the recall denominator."""
		return self.tp + self.fn

	@property
	def precision(self):
		"""TP / (TP + FP). No flags raised at all -> 1.0 (no false alarms by vacuity)."""
		flagged = self.tp + self.fp
		return 1.0 if flagged == 0 else self.tp / flagged

	@property
	def recall(self):
		"""TP / (TP + FN). No true positives to find -> 1.0 by convention; the self-validation
		ALWAYS supplies positives (one planted violation per attack vector), so a real run can
		never reach recall==1.0 vacuously — that is the whole point of audit M2."""
		return 1.0 if self.positives == 0 else self.tp / self.positives

	def as_dict(self):
		"""Flat dict for report.json / the PDF."""
		return {
			"tp": self.tp,
			"fp": self.fp,
			"fn": self.fn,
			"tn": self.tn,
			"positives": self.positives,
			"precision": round(self.precision, 4),
			"recall": round(self.recall, 4),
		}

	def summary(self):
		"""One-line human summary for the console."""
		return (
			"confusion: TP={0} FP={1} FN={2} TN={3} | precision={4:.3f} recall={5:.3f}".format(
				self.tp, self.fp, self.fn, self.tn, self.precision, self.recall
			)
		)
