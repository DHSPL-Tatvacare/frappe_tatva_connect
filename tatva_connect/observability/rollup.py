"""Roll the disposable raw log up into the immortal metric table.

ONE scheduled job (every 6h — see hooks.py `0 */6 * * *`). Three additive tiers, each an
idempotent INSERT ... ON DUPLICATE KEY UPDATE keyed on name = SHA1(grain):

    raw  (CRM API Request Log, ~7d)  -> 5min  (CRM API Metric, 90d)
    5min (CRM API Metric)            -> hour  (CRM API Metric, forever)
    hour (CRM API Metric)            -> day   (CRM API Metric, forever)

Only ADDITIVE primitives are stored (counts, sum/min/max latency, latency-band histogram),
so a bucket merges across time by SUM/MIN/MAX and a re-run over the same closed window
produces identical rows. Correctness does NOT depend on the advisory lock — the lock only
stops two workers duplicating work. The latency bands come from constants.LATENCY_BANDS
(single source of truth), not retyped here.

Windows are WHOLE-HOUR aligned on both ends: the run only finalizes complete hours ('to'
floors to the hour) and the purge drops whole hours ('_purge_5min' cuts on the hour). So an
hour's 5-min rows are all-present or all-gone — never partial — which means a coarse bucket
is only ever (re)built from a COMPLETE set of finer rows. A backfill can therefore never
overwrite an immortal hour/day with an undercount. Cost: the newest partial hour isn't
visible until it closes (accepted).
"""
import datetime as dt

import frappe
from frappe.utils import get_datetime, now_datetime

from tatva_connect import automation
from tatva_connect.observability.constants import HISTOGRAM_COLS, histogram_sql

_LOCK = "tc_obs_rollup"
RAW = "tabCRM API Request Log"
AGG = "tabCRM API Metric"
SETTINGS = "CRM API Metric Settings"
_JOB_METHOD = "tatva_connect.observability.rollup.run"

# Hours one tick will finalize. A cold watermark (first run, or a site that has not rolled for weeks) would
# otherwise aggregate the whole backlog in one job on a 300s scheduler budget; capped, the 6h cron catches
# up over the next few runs and every window stays whole-hour aligned.
MAX_WINDOW_HOURS = 24

# Non-histogram additive columns, then the histogram bands. Order == every SELECT below.
_BASE_COLS = (
	"request_count", "count_2xx", "count_3xx", "count_4xx", "count_5xx", "error_count",
	"sum_ms", "min_ms", "max_ms",
)
_METRIC_COLS = _BASE_COLS + HISTOGRAM_COLS
_COL_LIST = ", ".join(_METRIC_COLS)
_ON_DUP = ", ".join(f"{c}=VALUES({c})" for c in _METRIC_COLS) + ", modified=NOW(6)"

# Tier-1 (raw -> 5min): derive counts from status/latency; histogram from the bands.
_RAW_SELECT = ", ".join([
	"COUNT(*)",
	"SUM(status_code BETWEEN 200 AND 299)",
	"SUM(status_code BETWEEN 300 AND 399)",
	"SUM(status_code BETWEEN 400 AND 499)",
	"SUM(status_code BETWEEN 500 AND 599)",
	"SUM(is_error)",
	"SUM(duration_ms)", "MIN(duration_ms)", "MAX(duration_ms)",
	*histogram_sql("duration_ms"),
])

# Tiers 2 & 3 (metric -> coarser metric): pure additive re-aggregation of stored columns.
_AGG_SELECT = ", ".join([
	*(f"SUM({c})" for c in ("request_count", "count_2xx", "count_3xx", "count_4xx", "count_5xx", "error_count")),
	"SUM(sum_ms)", "MIN(min_ms)", "MAX(max_ms)",
	*(f"SUM({c})" for c in HISTOGRAM_COLS),
])


def run():
	"""Scheduler entrypoint. Idempotent; safe to run as often as you like."""
	if not automation.is_enabled("Observability::Metrics::rollup"):
		return  # operator kill-switch — ships dormant (invariant A.6), like every automation
	if frappe.db.sql(f"SELECT GET_LOCK('{_LOCK}', 0)")[0][0] != 1:  # sqli-ok: constant table/column identifiers (_LOCK/RAW/AGG/_COL_LIST/_RAW_SELECT/_ON_DUP); all values bound via %()s
		return  # another worker holds the lock — skip this tick
	try:
		lag = int(frappe.db.get_single_value(SETTINGS, "lag_seconds") or 300)
		retention = int(frappe.db.get_single_value(SETTINGS, "fine_grain_retention_days") or 90)

		# 'to' = floor(now - lag) to a WHOLE-HOUR boundary -> only ever finalize complete hours,
		# so no bucket is built (or rebuilt) from a partial set of finer rows. The lag still holds
		# finalisation back a touch past the hour close for late-arriving rows.
		to = now_datetime() - dt.timedelta(seconds=lag)
		to = to.replace(minute=0, second=0, microsecond=0)

		# 'from' = last watermark, else oldest surviving raw row, else 7 days back.
		# A zero/pre-epoch watermark ("0001-01-01") is truthy but breaks the coarse-tier
		# UNIX_TIMESTAMP() aggregation (NULL -> zero hour/day rows), so treat it as unset.
		frm = frappe.db.get_single_value(SETTINGS, "last_rolled_until")
		if frm and get_datetime(frm).year < 1970:
			frm = None
		if not frm:
			frm = frappe.db.sql(f"SELECT MIN(request_time) FROM `{RAW}`")[0][0]  # sqli-ok: constant table/column identifiers (_LOCK/RAW/AGG/_COL_LIST/_RAW_SELECT/_ON_DUP); all values bound via %()s
		frm = get_datetime(frm) if frm else (now_datetime() - dt.timedelta(days=7))
		# One bounded window per tick, floored like `to` above so both ends stay whole-hour aligned; the watermark carries the rest forward.
		to = min(to, (frm + dt.timedelta(hours=MAX_WINDOW_HOURS)).replace(minute=0, second=0, microsecond=0))

		log = f"nothing to do (window {frm} >= {to})"
		if to > frm:
			n5 = _rollup_raw_to_5min(frm, to)
			nh = _rollup_metric("5min", "hour", 3600, frm, to)
			nd = _rollup_metric("hour", "day", 86400, frm, to)
			purged = _purge_5min(retention)
			frappe.db.set_single_value(SETTINGS, "last_rolled_until", to)
			log = f"raw:{n5} hour-buckets:{nh} day-buckets:{nd} purged-5min:{purged} | {frm} -> {to}"

		frappe.db.set_single_value(SETTINGS, "last_run_at", now_datetime())
		frappe.db.set_single_value(SETTINGS, "last_run_log", log)
	finally:
		frappe.db.sql(f"SELECT RELEASE_LOCK('{_LOCK}')")  # sqli-ok: constant table/column identifiers (_LOCK/RAW/AGG/_COL_LIST/_RAW_SELECT/_ON_DUP); all values bound via %()s
		frappe.db.commit()


def _rollup_raw_to_5min(frm, to):
	"""Tier 1: raw request rows -> 5-min buckets over the closed window [frm, to)."""
	frappe.db.sql(  # sqli-ok: constant table/column identifiers (_LOCK/RAW/AGG/_COL_LIST/_RAW_SELECT/_ON_DUP); all values bound via %()s
		f"""
		INSERT INTO `{AGG}`
			(name, creation, modified, owner, modified_by, docstatus, idx,
			 bucket_start, granularity, channel, endpoint, source, {_COL_LIST})
		SELECT
			SHA1(CONCAT_WS('|', bs, '5min', channel, endpoint, source)),
			NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0,
			bs, '5min', channel, endpoint, source, {_RAW_SELECT}
		FROM (
			SELECT channel, endpoint, source, status_code, is_error, duration_ms,
				FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(request_time) / 300) * 300) AS bs
			FROM `{RAW}`
			WHERE request_time >= %(frm)s AND request_time < %(to)s
		) t
		GROUP BY bs, channel, endpoint, source
		ON DUPLICATE KEY UPDATE {_ON_DUP}
		""",
		{"frm": frm, "to": to},
	)
	return frappe.db.sql(  # sqli-ok: constant table/column identifiers (_LOCK/RAW/AGG/_COL_LIST/_RAW_SELECT/_ON_DUP); all values bound via %()s
		f"SELECT COUNT(*) FROM `{RAW}` WHERE request_time >= %(frm)s AND request_time < %(to)s",
		{"frm": frm, "to": to},
	)[0][0]


def _rollup_metric(src_gran, dst_gran, secs, frm, to):
	"""Tiers 2 & 3: re-aggregate finer metric rows into coarser buckets by SUM/MIN/MAX.
	Lower bound widened to the start of the dst-bucket containing `frm`, so a coarse bucket
	is always rebuilt from ALL its source rows, not just the new slice."""
	frappe.db.sql(  # sqli-ok: constant table/column identifiers (_LOCK/RAW/AGG/_COL_LIST/_RAW_SELECT/_ON_DUP); all values bound via %()s
		f"""
		INSERT INTO `{AGG}`
			(name, creation, modified, owner, modified_by, docstatus, idx,
			 bucket_start, granularity, channel, endpoint, source, {_COL_LIST})
		SELECT
			SHA1(CONCAT_WS('|', bs, %(dst)s, channel, endpoint, source)),
			NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0,
			bs, %(dst)s, channel, endpoint, source, {_AGG_SELECT}
		FROM (
			SELECT channel, endpoint, source, {_COL_LIST},
				FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(bucket_start) / %(secs)s) * %(secs)s) AS bs
			FROM `{AGG}`
			WHERE granularity = %(src)s
				AND bucket_start >= FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(%(frm)s) / %(secs)s) * %(secs)s)
				AND bucket_start < %(to)s
		) t
		GROUP BY bs, channel, endpoint, source
		ON DUPLICATE KEY UPDATE {_ON_DUP}
		""",
		{"src": src_gran, "dst": dst_gran, "secs": secs, "frm": frm, "to": to},
	)
	return frappe.db.sql(  # sqli-ok: constant table/column identifiers (_LOCK/RAW/AGG/_COL_LIST/_RAW_SELECT/_ON_DUP); all values bound via %()s
		f"""
		SELECT COUNT(*) FROM `{AGG}`
		WHERE granularity = %(dst)s
			AND bucket_start >= FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(%(frm)s) / %(secs)s) * %(secs)s)
			AND bucket_start < %(to)s
		""",
		{"dst": dst_gran, "frm": frm, "secs": secs, "to": to},
	)[0][0]


def _purge_5min(days):
	"""Drop fine-grain rows past the retention window, on a WHOLE-HOUR boundary so an hour's
	5-min rows are always all-present or all-gone — never partial. A coarse (hour/day) bucket
	can then only ever be rebuilt from a COMPLETE set of finer rows, so a backfill can never
	overwrite an immortal bucket with an undercount. Hourly/daily are immortal."""
	cutoff = (now_datetime() - dt.timedelta(days=int(days))).replace(minute=0, second=0, microsecond=0)
	frappe.db.sql(  # sqli-ok: constant table/column identifiers (_LOCK/RAW/AGG/_COL_LIST/_RAW_SELECT/_ON_DUP); all values bound via %()s
		f"DELETE FROM `{AGG}` WHERE granularity = '5min' AND bucket_start < %(c)s",
		{"c": cutoff},
	)
	return frappe.db.sql("SELECT ROW_COUNT()")[0][0]  # sqli-ok: constant query, no input — reads the DELETE's affected-row count (no frappe rowcount API).


def apply_rollup(enabled):
	"""Activate/deactivate the scheduled job by its Scheduled Job Type `stopped` flag, so a
	disabled rollup is never enqueued. Activator for Observability::Metrics::rollup."""
	name = frappe.db.get_value("Scheduled Job Type", {"method": _JOB_METHOD}, "name")
	if name:
		frappe.db.set_value("Scheduled Job Type", name, "stopped", 0 if enabled else 1)
