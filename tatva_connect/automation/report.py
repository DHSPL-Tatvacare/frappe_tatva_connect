# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Daily per-doctype Run Log summary — the read-only aggregation surface over `CRM Automation Run
Log` rows (Task 11). Reads ONLY; writes nothing (the rows themselves are written at fire time by
`dispatcher._write_run_log`, never seeded — invariant A.17).

IMPORTANT — the Run Log is fire-ONLY: a rule that evaluated but didn't match its criteria leaves no
row at all. So every count this report returns (`fired`, per-rule totals) counts FIRES, not
evaluations. A doctype with zero rows for a day may mean "nothing matched" or "nothing happened" —
this report cannot distinguish the two, by design (matching the log it reads).

Workspace-P2: grain-sliced aggregation for the custom Automations/Observability widgets
(`grain_health`, `grain_log_matrix`) + two Custom-Number-Card methods (`active_grains_card`,
`failure_rate_card`). Same posture as `daily_summary` throughout: `has_permission` FIRST (fail-closed,
no separate hole). `daily_summary`/`active_grains_card` use native `frappe.get_all` with
parameterised filters + pure-Python grouping (already bounded by day/rule-count); `grain_health`,
`grain_log_matrix`, and `failure_rate_card` instead `GROUP BY` in the DB via parameterised
`frappe.db.sql` (every value bound `%()s`, only constant table identifiers interpolated — S.2), so
their reads are bounded by grain/outcome/title cardinality, not fire count, over a wide window.
`grain` on Run Log is the dispatcher's own denormalized `vertical::group::program` tag
(`dispatcher._grain_tag`, stamped at fire time) — reused here, not re-derived (A.8), so these reports
never touch CRM Lead's permlevel-1 `custom_vertical/group/program` fields at all (Phase 1's open
item): the aggregate is already grain-safe without `ignore_permissions` anywhere in this module.
"""
import frappe
from frappe.utils import add_days, cint, getdate

from tatva_connect.automation.dispatcher import RUN_LOG, _grain_tag

OUTCOMES = ("Success", "Partial", "Failed")
NO_GRAIN = "(no grain)"

# Table identifiers for the grouped-aggregate queries below (`grain_health`, `grain_log_matrix`,
# `failure_rate_card`) — constants only, never built from request input; every filter VALUE in
# those queries is bound via `%()s` (S.2). `_RUN_LOG_TABLE` derives from the `RUN_LOG` doctype
# constant so the two never drift; `_ERROR_LOG_TABLE` names the core `Error Log` doctype (fixed).
_RUN_LOG_TABLE = f"tab{RUN_LOG}"
_ERROR_LOG_TABLE = "tabError Log"

# `grain_log_matrix`'s log SOURCES. Only `automation` (CRM Automation Run Log) carries a real
# `grain` field — that column is a true per-grain split. `partner`/`telephony`/`error` read the core
# Error Log, which has NO grain field at all; fabricating one would violate A.13/A.16, so every
# Error Log row lands in the single shared `NO_GRAIN` bucket instead, classified by title prefix
# (kept in sync with this app's actual `frappe.log_error(title=...)` call sites — see
# `telephony/adapter.py`, `telephony/reconcile.py`, `api/telephony.py`, `api/_base.py`). Anything
# that matches neither prefix tuple falls into the generic `error` column.
LOG_SOURCES = ("automation", "partner", "telephony", "error")
_TELEPHONY_TITLE_PREFIXES = ("telephony:", "Acefone")
_PARTNER_TITLE_PREFIXES = ("Partner API", "Idempotency release failed", "normalise_partner_response")
DEFAULT_WINDOW_DAYS = 7


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


def _window(days):
	"""[start, end) over the trailing `days` CALENDAR days, inclusive of today — the one window
	both `grain_health` and `grain_log_matrix` (and the two KPI cards) resolve through, so "7d"
	means the same 7 days everywhere on the dashboard (A.8)."""
	days = cint(days) or DEFAULT_WINDOW_DAYS
	end_day = getdate()
	start_day = add_days(end_day, -(days - 1))
	return f"{start_day} 00:00:00", f"{add_days(end_day, 1)} 00:00:00"


def _normalize_grain(grain):
	"""ONE rule for "no grain" shared by every endpoint in this module (Fix 3): `None`, blank, and
	the fully-empty `_grain_tag` output (`"::"` — vertical/group/program all unset) all collapse to
	the shared `NO_GRAIN` sentinel, so `active_grains_card` counts the same universe
	`grain_health`/`grain_log_matrix` bucket into."""
	return grain if grain and grain != "::" else NO_GRAIN


@frappe.whitelist()
def grain_health(days=None):
	"""Per-grain Run Log outcome counts over the trailing `days` window (default 7) — the source
	for the stacked health-by-grain bar. Same fire-only caveat as `daily_summary` (module
	docstring): a grain with zero rows may mean nothing matched, not nothing happened.

	Bounded by grain x outcome cardinality, not fire count: the count is a DB `GROUP BY`, so a
	30-day window with millions of fires still returns one row per (grain, outcome) pair."""
	frappe.has_permission(RUN_LOG, "read", throw=True)
	start, end = _window(days)
	rows = frappe.db.sql(  # sqli-ok: constant _RUN_LOG_TABLE identifier; start/end bound via %()s
		f"""SELECT grain, outcome, COUNT(*) AS cnt
			FROM `{_RUN_LOG_TABLE}`
			WHERE fire_time >= %(start)s AND fire_time < %(end)s
			GROUP BY grain, outcome""",
		{"start": start, "end": end},
		as_dict=True,
	)
	return _grain_totals(rows)


def _grain_totals(rows):
	"""Pure grouping over already-aggregated (grain, outcome, cnt) rows from the `GROUP BY` above —
	kept as a separate step so the returned shape stays parameter-safe and unit-testable in
	isolation, mirroring `_summarize`."""
	by_grain = {}
	for row in rows:
		grain = _normalize_grain(row["grain"])
		bucket = by_grain.setdefault(grain, {"grain": grain, "success": 0, "partial": 0, "failed": 0})
		bucket[row["outcome"].lower()] += row["cnt"]
	for bucket in by_grain.values():
		bucket["total"] = bucket["success"] + bucket["partial"] + bucket["failed"]
	return sorted(by_grain.values(), key=lambda b: b["total"], reverse=True)


def _classify_error_title(title):
	"""Bucket one Error Log title into `partner`/`telephony`/`error` — see the LOG_SOURCES
	docstring above for the prefix source list. Never returns `automation`: that column is Run
	Log's real per-grain count, not derived from Error Log at all."""
	if (title or "").startswith(_TELEPHONY_TITLE_PREFIXES):
		return "telephony"
	if (title or "").startswith(_PARTNER_TITLE_PREFIXES):
		return "partner"
	return "error"


@frappe.whitelist()
def grain_log_matrix(days=None):
	"""Per-grain counts by log SOURCE over the trailing `days` window (default 7) — the source for
	the grain x log-type heatmap. See the LOG_SOURCES module docstring for exactly which sources
	contribute and why only `automation` is grain-real.

	Fail-closed on Run Log (same gate as `grain_health`/`daily_summary`). Error Log itself is a
	core doctype with NO custom permission rows (System Manager only on this bench) — rather than
	throw for a Sales Manager who can read Run Log but not Error Log, this degrades: the
	`partner`/`telephony`/`error` columns read 0 and `error_log_readable: False` tells the caller
	why, so the widget can render a note instead of a hard error (partial data over a denial, the
	same posture the KPI/report surface already takes elsewhere).

	Bounded by grain cardinality (Run Log side) and DISTINCT error title cardinality (Error Log
	side), not fire/error count — both reads are DB `GROUP BY`s. `_classify_error_title` still runs
	in Python (A.8, unchanged), just once per distinct title rather than once per row."""
	frappe.has_permission(RUN_LOG, "read", throw=True)
	start, end = _window(days)

	run_rows = frappe.db.sql(  # sqli-ok: constant _RUN_LOG_TABLE identifier; start/end bound via %()s
		f"""SELECT grain, COUNT(*) AS cnt
			FROM `{_RUN_LOG_TABLE}`
			WHERE fire_time >= %(start)s AND fire_time < %(end)s
			GROUP BY grain""",
		{"start": start, "end": end},
		as_dict=True,
	)
	matrix = {}
	for row in run_rows:
		grain = _normalize_grain(row["grain"])
		bucket = matrix.setdefault(grain, dict.fromkeys(LOG_SOURCES, 0))
		bucket["automation"] += row["cnt"]

	error_log_readable = bool(frappe.has_permission("Error Log", "read"))
	if error_log_readable:
		# authz-ok: Error Log is read here ONLY behind the has_permission("Error Log","read") gate
		# on the line above (never unconditionally) — a caller without native Error Log read gets
		# error_log_readable=False and no rows are fetched, so this is a permission-respecting read
		# gated by an explicit check, not a bypass.
		err_rows = frappe.db.sql(  # sqli-ok: constant _ERROR_LOG_TABLE identifier; start/end bound via %()s
			f"""SELECT method, COUNT(*) AS cnt
				FROM `{_ERROR_LOG_TABLE}`
				WHERE creation >= %(start)s AND creation < %(end)s
				GROUP BY method""",
			{"start": start, "end": end},
			as_dict=True,
		)
		no_grain = matrix.setdefault(NO_GRAIN, dict.fromkeys(LOG_SOURCES, 0))
		for row in err_rows:
			no_grain[_classify_error_title(row["method"])] += row["cnt"]

	return {
		"sources": list(LOG_SOURCES),
		"rows": [{"grain": grain, **counts} for grain, counts in sorted(matrix.items())],
		"error_log_readable": error_log_readable,
	}


@frappe.whitelist()
def active_grains_card(filters=None):
	"""Custom Number Card: distinct grain across ENABLED automation rules (not fired grains — a
	rule can be enabled with zero fires yet). Reuses the dispatcher's own grain-tag formatter
	(A.8) so this always agrees with what Run Log rows actually carry."""
	frappe.has_permission("CRM Automation Rule", "read", throw=True)
	rows = frappe.get_all(
		"CRM Automation Rule",
		filters=[["enabled", "=", 1]],
		fields=["vertical", "group", "program"],
	)
	grains = {_normalize_grain(_grain_tag(r["vertical"], r["group"], r["program"])) for r in rows}
	return {"value": len(grains), "fieldtype": "Int"}


@frappe.whitelist()
def failure_rate_card(filters=None, days=None):
	"""Custom Number Card: Failed / total Run Log fires over the trailing `days` window (default
	7) — labelled "Failure Rate % (7d)" on the workspace. Same fire-only caveat as `daily_summary`:
	a zero-fire window reads as 0%, not "no automation activity".

	Bounded by outcome cardinality (3 rows max — Success/Partial/Failed), not fire count: the
	count is a DB `GROUP BY`."""
	frappe.has_permission(RUN_LOG, "read", throw=True)
	start, end = _window(days)
	rows = frappe.db.sql(  # sqli-ok: constant _RUN_LOG_TABLE identifier; start/end bound via %()s
		f"""SELECT outcome, COUNT(*) AS cnt
			FROM `{_RUN_LOG_TABLE}`
			WHERE fire_time >= %(start)s AND fire_time < %(end)s
			GROUP BY outcome""",
		{"start": start, "end": end},
		as_dict=True,
	)
	total = sum(r["cnt"] for r in rows)
	failed = sum(r["cnt"] for r in rows if r["outcome"] == "Failed")
	pct = round(failed / total * 100, 2) if total else 0
	return {"value": pct, "fieldtype": "Percent"}
