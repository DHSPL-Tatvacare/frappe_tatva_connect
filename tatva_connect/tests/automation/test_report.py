# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 11 sign-off - `automation.report.daily_summary`: the read-only daily per-doctype aggregation
over `CRM Automation Run Log` rows. Real Frappe types throughout.

Every fixture row is created via a real `frappe.get_doc(...).insert()` inside this FrappeTestCase
(never a raw console/SQL write - the hard safety constraint, see task-11-brief.md). This suite's own
`tearDownClass` explicitly deletes exactly what it created (by the `report-` rule-name prefix and the
`report-test-subject` Run Log marker), matching this repo's established convention for this test base
class (see `test_watch_entry.py`) - under the "old" `frappe.tests.utils.FrappeTestCase` runner this
app's suite uses, class-level fixture rows are NOT reliably rolled back (verified empirically), so
explicit narrow cleanup - not blind trust in auto-rollback - is what actually keeps the shared dev DB
clean. The assertions are still baseline-delta (not absolute counts): the shared Run Log table can
carry OTHER pre-existing rows for CRM Lead today from unrelated tests/sessions that are not this
suite's to touch.

Three legs:
  (a) 3-row aggregation - 2 rules, 2 Success + 1 Failed -> fired/outcome totals + per-rule breakdown.
  (b) window/doctype scoping planted-bad - a row on another doctype and a row on another day must NOT
      be counted (recall guard on the boundary, not just the happy path).
  (c) permission gate - a user without Run Log read is refused (S.1 fail-closed).
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, now_datetime

from tatva_connect.automation import report
from tatva_connect.automation.dispatcher import RUN_LOG
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

_RULE_DT = "CRM Automation Rule"
_GRAIN = GRAINS[0]
_RULE_NAME_PREFIX = "report-"  # every rule this suite creates is named "report-..."
_LOG_MARKER = "report-test-subject"  # every Run Log row this suite creates carries this trigger_docname


def _make_rule(name):
	"""Minimal grain-scoped rule, just enough to exist as the Run Log row's Link target - no actions,
	not wired to fire (this suite exercises the report over already-written rows, not the dispatcher)."""
	return frappe.get_doc(
		{
			"doctype": _RULE_DT,
			"rule_name": name,
			"enabled": 1,
			"on_doctype": "CRM Lead",
			"event": "Updated",
			"vertical": _GRAIN["vertical"],
			"group": _GRAIN["group"],
			"program": _GRAIN["program"],
		}
	).insert(ignore_permissions=True)


def _log_row(rule, outcome, trigger_doctype="CRM Lead", fire_time=None):
	return frappe.get_doc(
		{
			"doctype": RUN_LOG,
			"fire_time": fire_time or now_datetime(),
			"rule": rule,
			"trigger_doctype": trigger_doctype,
			"trigger_docname": _LOG_MARKER,
			"action_count": 1,
			"actions_success": 1 if outcome != "Failed" else 0,
			"actions_failed": 0 if outcome != "Failed" else 1,
			"outcome": outcome,
		}
	).insert(ignore_permissions=True)


def _cleanup():
	"""Delete exactly this suite's own rows - Run Log children first, then their parent rules - by the
	two narrow markers above. Never touches any other row (e.g. leaves any pre-existing/unrelated Run
	Log data on CRM Lead untouched, per the hard safety constraint)."""
	frappe.db.delete(RUN_LOG, {"trigger_docname": _LOG_MARKER})
	frappe.db.delete(_RULE_DT, {"rule_name": ["like", f"{_RULE_NAME_PREFIX}%"]})


class TestDailySummary(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		# Deliberately no super().setUpClass() call - matches this suite's established convention
		# (test_watch_entry.py): the base FrappeTestCase registers a class-exit `frappe.db.rollback()`
		# via addClassCleanup, but under this app's "old" test-runner category that rollback ends up
		# discarding this class's OWN explicit tearDownClass cleanup too (verified empirically - see
		# module docstring), which is worse than not registering it. Explicit cleanup is what actually
		# lands, so that's the only mechanism this suite relies on.
		assert_masters_exist()
		_cleanup()  # narrow, idempotent: also clears any leftover rows a prior interrupted run left behind

	@classmethod
	def tearDownClass(cls):
		_cleanup()

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	# (a) 2 Success (rule A) + 1 Failed (rule B) today -> fired=3, correct totals + per-rule breakdown.
	# Baseline-delta on the site-wide totals: the shared dev DB can carry pre-existing Run Log rows
	# for CRM Lead today from other test files/sessions (not this suite's to clean up - hard safety
	# constraint forbids touching them), so only the DELTA this test itself introduces is asserted
	# there; the per-rule buckets use this test's own uniquely-named rules, so those are exact.
	def test_three_row_aggregation(self):
		baseline = report.daily_summary("CRM Lead", str(getdate()))
		rule_a = _make_rule("report-agg-rule-a")
		rule_b = _make_rule("report-agg-rule-b")
		_log_row(rule_a.name, "Success")
		_log_row(rule_a.name, "Success")
		_log_row(rule_b.name, "Failed")

		out = report.daily_summary("CRM Lead", str(getdate()))

		self.assertEqual(out["doctype"], "CRM Lead")
		self.assertEqual(out["fired"] - baseline["fired"], 3)
		self.assertEqual(out["success"] - baseline["success"], 2)
		self.assertEqual(out["partial"] - baseline["partial"], 0)
		self.assertEqual(out["failed"] - baseline["failed"], 1)

		by_rule = {r["rule"]: r for r in out["rules"]}
		self.assertEqual(by_rule[rule_a.name]["outcome_success"], 2)
		self.assertEqual(by_rule[rule_a.name]["outcome_failed"], 0)
		self.assertEqual(by_rule[rule_b.name]["outcome_failed"], 1)
		# per-rule breakdown sorted by total fires desc - rule_a (2 fires) before rule_b (1 fire).
		self.assertLess(
			out["rules"].index(by_rule[rule_a.name]), out["rules"].index(by_rule[rule_b.name])
		)

	# (b) PLANTED-BAD: a row on a different doctype and a row on a different day must not be counted -
	# proves the window/doctype filter is real, not a pass-through. Baseline-delta for the same reason
	# as (a) - only this test's own insert may move the totals.
	def test_window_and_doctype_scoping_excludes_other_rows(self):
		baseline = report.daily_summary("CRM Lead", str(getdate()))
		rule = _make_rule("report-scope-rule")
		_log_row(rule.name, "Success")  # in-scope: today, CRM Lead
		_log_row(rule.name, "Success", trigger_doctype="CRM Task")  # wrong doctype
		yesterday = add_days(now_datetime(), -1)
		_log_row(rule.name, "Success", fire_time=yesterday)  # wrong day

		out = report.daily_summary("CRM Lead", str(getdate()))

		self.assertEqual(out["fired"] - baseline["fired"], 1)
		self.assertEqual(out["success"] - baseline["success"], 1)

	# (c) fail-closed: a user without CRM Automation Run Log read is refused, not silently emptied.
	def test_permission_gate_denies_unauthorized_user(self):
		try:
			frappe.set_user("Guest")
			with self.assertRaises(frappe.PermissionError):
				report.daily_summary("CRM Lead", str(getdate()))
		finally:
			frappe.set_user("Administrator")


if __name__ == "__main__":
	unittest.main()
