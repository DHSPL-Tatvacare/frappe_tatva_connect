"""Read-side analytics over CRM API Metric.

One place computes every derived metric (error rate, avg, p50/p95/p99) from the additive
columns, reused by BOTH consumers:
- the Dashboard Chart Source (time-series) -> `series()`
- the Custom Number Cards (single stat over a rolling window) -> the `*_card` methods

Percentiles come from constants.percentile_from_counts (the histogram single source of
truth). Every query pins ONE `granularity` — CRM API Metric holds 5min+hour+day rows, so an
unpinned aggregate would triple-count. Dimensions (channel/endpoint/source) are optional.
"""
import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from tatva_connect.observability.constants import HISTOGRAM_COLS, percentile_from_counts

AGG = "tabCRM API Metric"
DIMENSIONS = ("channel", "endpoint", "source")
METRICS = ("Requests", "Errors", "Error Rate", "Avg", "p50", "p95", "p99")

# SUM expressions for every additive column we read back.
_SUMS = "SUM(request_count) request_count, SUM(error_count) error_count, SUM(sum_ms) sum_ms, " + ", ".join(
	f"SUM({c}) {c}" for c in HISTOGRAM_COLS
)


def _where(granularity, filters):
	"""Build the WHERE on granularity + optional dimensions. Never an unpinned scan."""
	conds = ["granularity = %(granularity)s"]
	params = {"granularity": granularity or "day"}
	for dim in DIMENSIONS:
		val = (filters or {}).get(dim)
		if val:
			conds.append(f"{dim} = %({dim})s")
			params[dim] = val
	return conds, params


def _value(metric, row):
	"""Compute one derived metric from an aggregated row (dict-like)."""
	rc = row.get("request_count") or 0
	if metric == "Requests":
		return rc
	if metric == "Errors":
		return row.get("error_count") or 0
	if metric == "Error Rate":
		return round((row.get("error_count") or 0) / rc * 100, 2) if rc else 0
	if metric == "Avg":
		return round((row.get("sum_ms") or 0) / rc, 1) if rc else 0
	if metric in ("p50", "p95", "p99"):
		return percentile_from_counts(row, rc, int(metric[1:]))
	frappe.throw(f"Unknown metric: {metric}")


def _window(timespan=None, from_date=None, to_date=None):
	"""Resolve a [from, to) window from a Dashboard Chart timespan or explicit dates."""
	if from_date and to_date:
		return get_datetime(from_date), get_datetime(to_date)
	days = {"Last Week": 7, "Last Month": 30, "Last Quarter": 92, "Last Year": 366}.get(timespan or "Last Month", 30)
	to = now_datetime()
	return add_to_date(to, days=-days), to


# ── Dashboard Chart Source ────────────────────────────────────────────────────────────

def series(metric, granularity, frm, to, filters=None):
	"""Time-series points for one metric: returns (labels, values) over [frm, to)."""
	conds, params = _where(granularity, filters)
	params.update(frm=frm, to=to)
	rows = frappe.db.sql(  # sqli-ok: constant AGG table + _SUMS/DIMENSIONS identifiers; filter values bound via %()s
		f"""SELECT bucket_start, {_SUMS}
			FROM `{AGG}`
			WHERE {' AND '.join(conds)} AND bucket_start >= %(frm)s AND bucket_start < %(to)s
			GROUP BY bucket_start ORDER BY bucket_start""",
		params,
		as_dict=True,
	)
	labels = [str(r.bucket_start) for r in rows]
	values = [_value(metric, r) for r in rows]
	return labels, values


# ── Custom Number Cards (rolling window over hourly rows) ─────────────────────────────

def _card(metric, filters, hours, fieldtype):
	conds, params = _where("hour", frappe.parse_json(filters) if isinstance(filters, str) else filters)
	params["frm"] = add_to_date(now_datetime(), hours=-hours)
	row = frappe.db.sql(  # sqli-ok: constant AGG table + _SUMS/DIMENSIONS identifiers; filter values bound via %()s
		f"SELECT {_SUMS} FROM `{AGG}` WHERE {' AND '.join(conds)} AND bucket_start >= %(frm)s",
		params,
		as_dict=True,
	)[0]
	return {"value": _value(metric, row), "fieldtype": fieldtype}


@frappe.whitelist()
def error_rate_card(filters=None):
	"""Error rate % over the last 24h (rolling, hourly grain)."""
	return _card("Error Rate", filters, 24, "Percent")


@frappe.whitelist()
def p95_card(filters=None):
	"""p95 latency (ms) over the last 24h."""
	return _card("p95", filters, 24, "Float")


@frappe.whitelist()
def p99_card(filters=None):
	"""p99 latency (ms) over the last 24h."""
	return _card("p99", filters, 24, "Float")


@frappe.whitelist()
def avg_latency_card(filters=None):
	"""Average latency (ms) over the last 24h."""
	return _card("Avg", filters, 24, "Float")
