"""One row per lead pool per hour: how many leads wait in it for someone with room to take them."""
import frappe

from tatva_connect.automation.actions import POOL_SIGNAL
from tatva_connect.tatva_analytics import facts
from tatva_connect.tatva_analytics.models import lead_allocation_fact

JOB = "pool_backlog_summary"
TARGET = "Tatva Pool Backlog Summary"
AFTER = lead_allocation_fact.JOB
# A snapshot reads the backlog as it stands, so it has no window to load.
WINDOW_DAYS = 0
# One snapshot per pool per hour: every run that hour updates it, and earlier hours are kept.
PERIOD = "hour"
PROBE = {"signal": "", "snapshot": "1900-01-01 00:00:00"}


def select_sql():
	"""Each lead pool with its business line and the runs parked on it waiting for room (the indexed `awaiting_signal` first)."""
	return f"""SELECT {facts.grain_key("r.name", "%(snapshot)s")} AS grain_key, r.name AS pool, %(snapshot)s AS snapshot, 1 AS is_latest,
       r.grain_vertical, r.grain_group, r.grain_program, COALESCE(w.waiting, 0) AS waiting
FROM `tabAssignment Rule` r
LEFT JOIN (SELECT awaiting_correlation AS pool, COUNT(*) AS waiting FROM `tabCRM Workflow Journey`
           WHERE awaiting_signal = %(signal)s AND status = 'Parked' GROUP BY awaiting_correlation) w ON w.pool = r.name
WHERE r.document_type = 'CRM Lead'"""


def refresh(run):
	"""This hour's backlog of every pool, which alone reads as latest."""
	snapshot = facts.period_start(run.started, PERIOD)
	rows = frappe.db.sql(select_sql(), {"signal": POOL_SIGNAL, "snapshot": snapshot}, as_dict=True)  # sqli-ok: the model's own SQL, values bound
	facts.clear_latest(TARGET)
	return {"changed": len(rows), "written": run.write(TARGET, rows)}
