# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Daily per-doctype Run Log summary — the read-only aggregation surface over `CRM Automation Run
Log` rows (Task 11). Reads ONLY; writes nothing (the rows themselves are written at fire time by
`dispatcher._write_run_log`, never seeded — invariant A.17).

IMPORTANT — the Run Log is fire-ONLY: a rule that evaluated but didn't match its criteria leaves no
row at all. So every count this report returns (`fired`, per-rule totals) counts FIRES, not
evaluations. A doctype with zero rows for a day may mean "nothing matched" or "nothing happened" —
this report cannot distinguish the two, by design (matching the log it reads).
"""
import frappe
from frappe.utils import add_days, getdate

from tatva_connect.automation.dispatcher import RUN_LOG

OUTCOMES = ("Success", "Partial", "Failed")


@frappe.whitelist()
def daily_summary(doctype, date=None):
	"""Fires of every automation rule on `doctype` during the day `date` (default today), grouped by
	outcome and by rule. Fail-closed: a caller who cannot read the Run Log cannot read this report
	either — same permission surface, no separate hole."""
	frappe.has_permission(RUN_LOG, "read", throw=True)

	day = getdate(date)
	window_start = f"{day} 00:00:00"
	window_end = f"{add_days(day, 1)} 00:00:00"

	rows = frappe.get_all(
		RUN_LOG,
		filters=[
			["trigger_doctype", "=", doctype],
			["fire_time", ">=", window_start],
			["fire_time", "<", window_end],
		],
		fields=["rule", "outcome"],
	)
	return _summarize(doctype, day, rows)


def _summarize(doctype, day, rows):
	"""Pure grouping over already-fetched rows — kept out of SQL so the aggregation stays
	parameter-safe and easy to unit-test in isolation."""
	by_rule = {}
	totals = dict.fromkeys(OUTCOMES, 0)
	for row in rows:
		outcome = row["outcome"]
		totals[outcome] = totals.get(outcome, 0) + 1
		bucket = by_rule.setdefault(
			row["rule"], {"rule": row["rule"], "outcome_success": 0, "outcome_partial": 0, "outcome_failed": 0}
		)
		bucket[f"outcome_{outcome.lower()}"] += 1

	rules = sorted(
		by_rule.values(),
		key=lambda b: b["outcome_success"] + b["outcome_partial"] + b["outcome_failed"],
		reverse=True,
	)

	return {
		"doctype": doctype,
		"date": str(day),
		"fired": len(rows),
		"success": totals["Success"],
		"partial": totals["Partial"],
		"failed": totals["Failed"],
		"rules": rules,
	}
