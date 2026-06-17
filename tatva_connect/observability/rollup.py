"""Roll the disposable raw log up into the immortal metric table.

ONE scheduled job (every 6h — see hooks.py `0 */6 * * *`). Three additive tiers, each
an idempotent INSERT ... ON DUPLICATE KEY UPDATE keyed on name = SHA1(grain):

    raw  (CRM API Request Log, ~7d)  -> 5min  (CRM API Metric, 90d)
    5min (CRM API Metric)            -> hour  (CRM API Metric, forever)
    hour (CRM API Metric)            -> day   (CRM API Metric, forever)

Only ADDITIVE primitives are stored (counts, sum/min/max latency, fixed latency-band
histogram), so a bucket merges across time by SUM/MIN/MAX and a re-run over the same
closed window produces identical rows. Correctness therefore does NOT depend on the
advisory lock — the lock only stops two workers from duplicating work.

Percentiles/rates are NOT stored; they are derived at read time from the histogram
(p95 = interpolate across the cumulative bands), exactly like Prometheus
`histogram_quantile`. That is the whole reason the schema is additive.
"""
import datetime as dt

import frappe
from frappe.utils import get_datetime, now_datetime

_LOCK = "tc_obs_rollup"
RAW = "tabCRM API Request Log"
AGG = "tabCRM API Metric"
SETTINGS = "CRM API Metric Settings"

# Metric columns, in the exact order every SELECT below produces them.
_METRIC_COLS = (
	"request_count", "count_2xx", "count_3xx", "count_4xx", "count_5xx", "error_count",
	"sum_ms", "min_ms", "max_ms",
	"b10", "b25", "b50", "b100", "b250", "b500", "b1000", "b2500", "b5000", "b_inf",
)
_COL_LIST = ", ".join(_METRIC_COLS)
_ON_DUP = ", ".join(f"{c}=VALUES({c})" for c in _METRIC_COLS) + ", modified=NOW(6)"


def run():
	"""Scheduler entrypoint. Idempotent; safe to run as often as you like."""
	if frappe.db.sql(f"SELECT GET_LOCK('{_LOCK}', 0)")[0][0] != 1:
		return  # another worker holds the lock — skip this tick
	try:
		lag = int(frappe.db.get_single_value(SETTINGS, "lag_seconds") or 300)
		retention = int(frappe.db.get_single_value(SETTINGS, "fine_grain_retention_days") or 90)

		# 'to' = floor(now - lag) to a 5-min boundary -> never finalize an in-flight bucket.
		to = now_datetime() - dt.timedelta(seconds=lag)
		to = to.replace(second=0, microsecond=0)
		to = to - dt.timedelta(minutes=to.minute % 5)

		# 'from' = last watermark, else oldest surviving raw row, else 7 days back.
		frm = frappe.db.get_single_value(SETTINGS, "last_rolled_until")
		if not frm:
			frm = frappe.db.sql(f"SELECT MIN(request_time) FROM `{RAW}`")[0][0]
		frm = get_datetime(frm) if frm else (now_datetime() - dt.timedelta(days=7))

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
		frappe.db.sql(f"SELECT RELEASE_LOCK('{_LOCK}')")
		frappe.db.commit()


def _rollup_raw_to_5min(frm, to):
	"""Tier 1: raw request rows -> 5-min buckets. Counts/histogram computed from status
	and latency; the closed window [frm, to) is immutable so the upsert is idempotent."""
	frappe.db.sql(
		f"""
		INSERT INTO `{AGG}`
			(name, creation, modified, owner, modified_by, docstatus, idx,
			 bucket_start, granularity, channel, endpoint, source, {_COL_LIST})
		SELECT
			SHA1(CONCAT_WS('|', bs, '5min', channel, endpoint, source)),
			NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0,
			bs, '5min', channel, endpoint, source,
			COUNT(*),
			SUM(status_code BETWEEN 200 AND 299),
			SUM(status_code BETWEEN 300 AND 399),
			SUM(status_code BETWEEN 400 AND 499),
			SUM(status_code BETWEEN 500 AND 599),
			SUM(is_error),
			SUM(duration_ms), MIN(duration_ms), MAX(duration_ms),
			SUM(duration_ms <= 10),
			SUM(duration_ms > 10 AND duration_ms <= 25),
			SUM(duration_ms > 25 AND duration_ms <= 50),
			SUM(duration_ms > 50 AND duration_ms <= 100),
			SUM(duration_ms > 100 AND duration_ms <= 250),
			SUM(duration_ms > 250 AND duration_ms <= 500),
			SUM(duration_ms > 500 AND duration_ms <= 1000),
			SUM(duration_ms > 1000 AND duration_ms <= 2500),
			SUM(duration_ms > 2500 AND duration_ms <= 5000),
			SUM(duration_ms > 5000)
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
	return frappe.db.sql(
		f"SELECT COUNT(*) FROM `{RAW}` WHERE request_time >= %(frm)s AND request_time < %(to)s",
		{"frm": frm, "to": to},
	)[0][0]


def _rollup_metric(src_gran, dst_gran, secs, frm, to):
	"""Tiers 2 & 3: re-aggregate finer metric rows into coarser buckets by SUM/MIN/MAX.
	The lower bound is widened to the start of the dst-bucket that contains `frm`, so a
	coarse bucket is always rebuilt from ALL its source rows, not just the new slice."""
	frappe.db.sql(
		f"""
		INSERT INTO `{AGG}`
			(name, creation, modified, owner, modified_by, docstatus, idx,
			 bucket_start, granularity, channel, endpoint, source, {_COL_LIST})
		SELECT
			SHA1(CONCAT_WS('|', bs, %(dst)s, channel, endpoint, source)),
			NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0,
			bs, %(dst)s, channel, endpoint, source,
			SUM(request_count),
			SUM(count_2xx), SUM(count_3xx), SUM(count_4xx), SUM(count_5xx), SUM(error_count),
			SUM(sum_ms), MIN(min_ms), MAX(max_ms),
			SUM(b10), SUM(b25), SUM(b50), SUM(b100), SUM(b250), SUM(b500),
			SUM(b1000), SUM(b2500), SUM(b5000), SUM(b_inf)
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
	return frappe.db.sql(
		f"""
		SELECT COUNT(*) FROM `{AGG}`
		WHERE granularity = %(dst)s
			AND bucket_start >= FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(%(frm)s) / %(secs)s) * %(secs)s)
			AND bucket_start < %(to)s
		""",
		{"dst": dst_gran, "frm": frm, "secs": secs, "to": to},
	)[0][0]


def _purge_5min(days):
	"""Drop fine-grain rows past the retention window. Hourly/daily are immortal."""
	cutoff = now_datetime() - dt.timedelta(days=int(days))
	frappe.db.sql(
		f"DELETE FROM `{AGG}` WHERE granularity = '5min' AND bucket_start < %(c)s",
		{"c": cutoff},
	)
	return frappe.db.sql("SELECT ROW_COUNT()")[0][0]
