"""The pieces every analytics job shares: where a run reads from, the grain key, deleted sources, and snapshot periods (docs/conventions/backend/analytics-facts.md)."""
from datetime import timedelta

import frappe
from frappe.utils import add_days, get_datetime, getdate

# Every incremental read starts this far before the watermark: a row commits a moment after it stamps its time.
OVERLAP = timedelta(minutes=5)
# dbt's own placeholder for a NULL grain column, so a NULL and an empty string never share a key.
GRAIN_NULL = "_dbt_utils_surrogate_key_null_"


def grain_key(*columns):
	"""The dbt surrogate key of a row's grain columns, as SQL: MD5 over the values joined by '-', each NULL replaced by dbt's placeholder."""
	parts = ", ".join(f"COALESCE(CAST({column} AS CHAR), '{GRAIN_NULL}')" for column in columns)
	return f"MD5(CONCAT_WS('-', {parts}))"


def read_from(run):
	"""Where a run reads its sources from: the watermark less the overlap, or the window back on a first load or Rebuild."""
	return get_datetime(run.watermark) - OVERLAP if run.watermark else get_datetime(add_days(run.started, -run.window_days))


def deleted_since(doctype, since, until):
	"""Records of `doctype` deleted in since..until as (name, the record as it was), read from Deleted Document by its creation index."""
	rows = frappe.get_all("Deleted Document", fields=["deleted_name", "data"],
	                      filters={"creation": ("between", (since, until)), "deleted_doctype": doctype})
	return [(row.deleted_name, frappe._dict(frappe.parse_json(row.data) or {})) for row in rows]


def period_start(moment, period):
	"""The start of the hour or the day holding `moment`: the snapshot every run inside that period writes to."""
	moment = get_datetime(moment)
	return moment.replace(minute=0, second=0, microsecond=0) if period == "hour" else getdate(moment)


def clear_latest(doctype):
	"""Mark no row latest; the run then writes this period's rows with is_latest = 1, in the same transaction."""
	table = frappe.qb.DocType(doctype)
	frappe.qb.update(table).set(table.is_latest, 0).where(table.is_latest == 1).run()
