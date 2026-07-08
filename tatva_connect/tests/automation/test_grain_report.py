# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Workspace-P2 sign-off - the 4 grain-report endpoints added in 508dad6: `report.grain_health`,
`report.grain_log_matrix`, `report.active_grains_card`, `report.failure_rate_card`. Same posture
and hard safety constraints as `test_report.py` (see its module docstring): every fixture row is a
real `frappe.get_doc(...).insert()` inside this FrappeTestCase, this suite's own tearDownClass
explicitly deletes exactly what it created (this app's "old" FrappeTestCase runner does not
reliably auto-rollback class-level fixtures - verified empirically by `test_report.py`), and
assertions are baseline-delta, not absolute counts (the shared Run Log / Error Log / Automation
Rule tables can carry other suites' rows). Real Frappe types throughout.

Four legs, one per endpoint under test, each with a PLANTED-BAD case that proves the grouping
isn't blind (S.6 posture - never trust a report that only shows the happy path):
  (a) grain_health - 2 grains x mixed outcomes -> per-grain success/partial/failed/total; a row on
      grain B must land in B's bucket, not bleed into A's.
  (b) grain_log_matrix - automation-source counts come from Run Log's `grain` column; the
      partner/telephony/error columns come from Error Log title-prefix classification, landing in
      the shared NO_GRAIN row; an unrelated title must fall into the generic `error` bucket, not
      `partner`/`telephony`.
  (c) active_grains_card - distinct grain count across ENABLED rules only; a second enabled rule in
      the SAME grain must not inflate the count, a DISABLED rule in a new grain must not count.
  (d) failure_rate_card - Failed/total percentage on known inputs; a Failed row OUTSIDE the window
      must not move the ratio.
Plus the has_permission fail-closed gate (S.1) for all four whitelisted endpoints.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

from tatva_connect.automation import report
from tatva_connect.automation.dispatcher import RUN_LOG, _grain_tag
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

_RULE_DT = "CRM Automation Rule"
_GRAIN_A = GRAINS[0]  # GoodFlip Care::Anaya::Nivolumab
_GRAIN_B = GRAINS[1]  # GoodFlip Care::Anaya::Tukavo - shares vertical+group with A, differs on program
_GRAIN_C = GRAINS[2]  # TatvaPractice::India::FieldSales - a third, unrelated grain
_TAG_A = _grain_tag(_GRAIN_A["vertical"], _GRAIN_A["group"], _GRAIN_A["program"])
_TAG_B = _grain_tag(_GRAIN_B["vertical"], _GRAIN_B["group"], _GRAIN_B["program"])
_TAG_C = _grain_tag(_GRAIN_C["vertical"], _GRAIN_C["group"], _GRAIN_C["program"])

_RULE_NAME_PREFIX = "grtest-"  # every rule this suite creates is named "grtest-..." (distinct from
# test_report.py's "report-" prefix, so the two suites' cleanups never collide)
_LOG_MARKER = "grtest-subject"  # every Run Log row this suite creates carries this trigger_docname
_ERROR_MARKER = "grtest-error-subject"  # every Error Log row this suite creates carries this reference_name


def _make_rule(name, grain, enabled=1):
	"""Minimal grain-scoped rule - just enough to exist as a Link target / to be counted by
	`active_grains_card`. Not wired to fire (this suite exercises the reports over already-written
	rows / already-created rules, not the dispatcher)."""
	return frappe.get_doc(
		{
			"doctype": _RULE_DT,
			"rule_name": name,
			"enabled": enabled,
			"on_doctype": "CRM Lead",
			"event": "Updated",
			"vertical": grain["vertical"],
			"group": grain["group"],
			"program": grain["program"],
		}
	).insert(ignore_permissions=True)


def _log_row(rule, outcome, grain_tag, fire_time=None):
	return frappe.get_doc(
		{
			"doctype": RUN_LOG,
			"fire_time": fire_time or now_datetime(),
			"rule": rule,
			"trigger_doctype": "CRM Lead",
			"trigger_docname": _LOG_MARKER,
			"grain": grain_tag,
			"action_count": 1,
			"actions_success": 1 if outcome != "Failed" else 0,
			"actions_failed": 0 if outcome != "Failed" else 1,
			"outcome": outcome,
		}
	).insert(ignore_permissions=True)


def _error_row(title):
	"""A real Error Log row via `frappe.log_error` (the exact codepath `_classify_error_title`
	classifies) - `title` lands in the `method` field, tagged with `_ERROR_MARKER` for narrow
	cleanup."""
	return frappe.log_error(
		title=title,
		message="grtest synthetic error - safe to delete",
		reference_doctype=RUN_LOG,
		reference_name=_ERROR_MARKER,
	)


def _bucket(rows, key, value):
	"""First row whose `key` == `value`, or {} if absent - so callers can `.get(field, 0)` a
	baseline that may not have this grain/source yet."""
	return next((r for r in rows if r[key] == value), {})


def _cleanup():
	"""Delete exactly this suite's own rows - Run Log rows, Error Log rows, then the rules - by the
	three narrow markers above. Never touches any other row."""
	frappe.db.delete(RUN_LOG, {"trigger_docname": _LOG_MARKER})
	frappe.db.delete("Error Log", {"reference_doctype": RUN_LOG, "reference_name": _ERROR_MARKER})
	frappe.db.delete(_RULE_DT, {"rule_name": ["like", f"{_RULE_NAME_PREFIX}%"]})


class TestGrainReports(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		# Deliberately no super().setUpClass() call - matches test_report.py's established
		# convention: the base FrappeTestCase's class-exit rollback ends up discarding this class's
		# own explicit tearDownClass cleanup too under this app's test-runner (verified empirically
		# there), so explicit cleanup is the only mechanism this suite relies on.
		assert_masters_exist()
		_cleanup()  # narrow, idempotent: also clears any leftover rows a prior interrupted run left behind

	@classmethod
	def tearDownClass(cls):
		_cleanup()

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	# (a) grain_health: grain A gets 2 Success + 1 Partial + 1 Failed, grain B gets 1 Success.
	# PLANTED-BAD: grain B's single Success must land in B's bucket, not bleed into A's success
	# count / A's total - if grain grouping were broken (e.g. keyed on program alone, or all rows
	# fell into one bucket), B's total would read 5 instead of 1, or A's success would read 3.
	def test_grain_health_per_grain_outcome_counts(self):
		baseline = report.grain_health(days=1)
		base_a = _bucket(baseline, "grain", _TAG_A)
		base_b = _bucket(baseline, "grain", _TAG_B)

		rule_a = _make_rule("grtest-health-a", _GRAIN_A)
		rule_b = _make_rule("grtest-health-b", _GRAIN_B)
		_log_row(rule_a.name, "Success", _TAG_A)
		_log_row(rule_a.name, "Success", _TAG_A)
		_log_row(rule_a.name, "Partial", _TAG_A)
		_log_row(rule_a.name, "Failed", _TAG_A)
		_log_row(rule_b.name, "Success", _TAG_B)

		out = report.grain_health(days=1)
		bucket_a = _bucket(out, "grain", _TAG_A)
		bucket_b = _bucket(out, "grain", _TAG_B)

		self.assertEqual(bucket_a["success"] - base_a.get("success", 0), 2)
		self.assertEqual(bucket_a["partial"] - base_a.get("partial", 0), 1)
		self.assertEqual(bucket_a["failed"] - base_a.get("failed", 0), 1)
		self.assertEqual(bucket_a["total"] - base_a.get("total", 0), 4)
		# planted-bad: grain B's row must not have inflated grain A's counts, and must show up
		# correctly in grain B's own bucket instead.
		self.assertEqual(bucket_b["success"] - base_b.get("success", 0), 1)
		self.assertEqual(bucket_b["total"] - base_b.get("total", 0), 1)

	# (b) grain_log_matrix: 2 automation fires on grain A + 3 Error Log rows with distinct title
	# prefixes. PLANTED-BAD: the automation fires must not bleed into A's partner/telephony/error
	# columns, and an unrelated title must fall into the generic `error` bucket, not `telephony`/
	# `partner` - proving the prefix classifier isn't a pass-through that buckets everything together.
	def test_grain_log_matrix_buckets_by_source(self):
		baseline = report.grain_log_matrix(days=1)
		base_rows = {r["grain"]: r for r in baseline["rows"]}
		base_a = base_rows.get(_TAG_A, dict.fromkeys(report.LOG_SOURCES, 0))
		base_no_grain = base_rows.get(report.NO_GRAIN, dict.fromkeys(report.LOG_SOURCES, 0))

		rule_a = _make_rule("grtest-matrix-a", _GRAIN_A)
		_log_row(rule_a.name, "Success", _TAG_A)
		_log_row(rule_a.name, "Success", _TAG_A)

		_error_row("telephony: grtest bridge fail")
		_error_row("Partner API grtest sync fail")
		_error_row("grtest totally unrelated crash")  # must land in generic 'error'

		out = report.grain_log_matrix(days=1)
		self.assertTrue(out["error_log_readable"])  # Administrator can read Error Log
		rows = {r["grain"]: r for r in out["rows"]}
		bucket_a = rows[_TAG_A]
		bucket_no_grain = rows[report.NO_GRAIN]

		self.assertEqual(bucket_a["automation"] - base_a.get("automation", 0), 2)
		# planted-bad: grain A's automation fires must not have bled into any other source column.
		self.assertEqual(bucket_a["partner"] - base_a.get("partner", 0), 0)
		self.assertEqual(bucket_a["telephony"] - base_a.get("telephony", 0), 0)
		self.assertEqual(bucket_a["error"] - base_a.get("error", 0), 0)

		self.assertEqual(bucket_no_grain["telephony"] - base_no_grain.get("telephony", 0), 1)
		self.assertEqual(bucket_no_grain["partner"] - base_no_grain.get("partner", 0), 1)
		# planted-bad: the unrelated title must fall into the generic 'error' bucket, not get
		# misclassified as telephony/partner, and NO_GRAIN's automation column stays untouched
		# (Error Log rows never count as 'automation').
		self.assertEqual(bucket_no_grain["error"] - base_no_grain.get("error", 0), 1)
		self.assertEqual(bucket_no_grain["automation"] - base_no_grain.get("automation", 0), 0)

	# (c) active_grains_card: distinct grain count across ENABLED rules only. Each assertion is a
	# delta against the immediately-prior call (not a fresh baseline), so the test is correct
	# regardless of what other suites' rules already exist on the shared DB.
	def test_active_grains_card_counts_distinct_enabled_grains(self):
		after_zero = report.active_grains_card()["value"]

		_make_rule("grtest-card-a1", _GRAIN_A, enabled=1)
		after_first = report.active_grains_card()["value"]
		self.assertEqual(after_first, after_zero + 1)

		# PLANTED-BAD: a second ENABLED rule in the SAME grain must not inflate the distinct count.
		_make_rule("grtest-card-a2", _GRAIN_A, enabled=1)
		after_same_grain = report.active_grains_card()["value"]
		self.assertEqual(after_same_grain, after_first)

		# PLANTED-BAD: a DISABLED rule in a NEW grain must not be counted at all.
		rule_c = _make_rule("grtest-card-c-disabled", _GRAIN_C, enabled=0)
		after_disabled_new_grain = report.active_grains_card()["value"]
		self.assertEqual(after_disabled_new_grain, after_same_grain)

		# Flipping that same rule to enabled adds exactly one NEW distinct grain.
		rule_c.enabled = 1
		rule_c.save(ignore_permissions=True)
		after_enabled_new_grain = report.active_grains_card()["value"]
		self.assertEqual(after_enabled_new_grain, after_same_grain + 1)
		self.assertEqual(report.active_grains_card()["fieldtype"], "Int")

	# (d) failure_rate_card: Failed/total percentage on known inputs, cross-checked against
	# `grain_health`'s own totals over the identical window (same table, same filters - both
	# endpoints must agree). PLANTED-BAD: a Failed row OUTSIDE the days=1 window must not move the
	# ratio - proves the window filter is real, not decorative.
	def test_failure_rate_card_ratio(self):
		def _window_totals():
			buckets = report.grain_health(days=1)
			return sum(b["total"] for b in buckets), sum(b["failed"] for b in buckets)

		base_total, base_failed = _window_totals()

		rule = _make_rule("grtest-rate-a", _GRAIN_A)
		_log_row(rule.name, "Success", _TAG_A)
		_log_row(rule.name, "Success", _TAG_A)
		_log_row(rule.name, "Success", _TAG_A)
		_log_row(rule.name, "Failed", _TAG_A)
		yesterday = add_days(now_datetime(), -1)
		_log_row(rule.name, "Failed", _TAG_A, fire_time=yesterday)  # outside the days=1 window

		total, failed = _window_totals()
		self.assertEqual(total - base_total, 4)  # yesterday's Failed row excluded
		self.assertEqual(failed - base_failed, 1)  # yesterday's Failed row excluded

		expected_pct = round(failed / total * 100, 2) if total else 0
		out = report.failure_rate_card(days=1)
		self.assertEqual(out["value"], expected_pct)
		self.assertEqual(out["fieldtype"], "Percent")

	# (e) fail-closed: a user without the required read is refused, not silently emptied, on ALL
	# four endpoints - same posture as test_report.py's daily_summary gate (S.1).
	def test_permission_gate_denies_unauthorized_user(self):
		try:
			frappe.set_user("Guest")
			with self.assertRaises(frappe.PermissionError):
				report.grain_health()
			with self.assertRaises(frappe.PermissionError):
				report.grain_log_matrix()
			with self.assertRaises(frappe.PermissionError):
				report.active_grains_card()
			with self.assertRaises(frappe.PermissionError):
				report.failure_rate_card()
		finally:
			frappe.set_user("Administrator")


if __name__ == "__main__":
	unittest.main()
