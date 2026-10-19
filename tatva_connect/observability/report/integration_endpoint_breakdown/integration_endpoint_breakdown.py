# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One row per endpoint for one integration or lane since a time, so a partner's failure reads by endpoint and cause."""
import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

LOG = "CRM API Request Log"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(LOG, "read", throw=True)
	values = {"from_datetime": filters.from_datetime or add_to_date(now_datetime(), days=-1)}
	conditions = ["request_time >= %(from_datetime)s"]
	# A blank filter means every integration or lane, so it adds no condition at all.
	if filters.integration:
		conditions.append("source = %(integration)s")
		values["integration"] = filters.integration
	if filters.lane:
		conditions.append("channel = %(lane)s")
		values["lane"] = filters.lane
	rows = frappe.db.sql(  # sqli-ok: constant column list and WHERE fragments; every value bound via %()s
		f"""
		SELECT
			endpoint,
			COUNT(*) AS calls,
			SUM(is_error) AS errors,
			SUM(error_code = 'rate_limited') AS rate_limited,
			SUM(error_code IN ('validation_error', 'bad_request')) AS bad_payload,
			SUM(error_code IN ('unauthorized', 'forbidden')) AS auth,
			SUM(error_code IN ('server_error', 'server_busy')) AS our_fault,
			SUM(error_code = 'not_found') AS not_found,
			ROUND(AVG(duration_ms)) AS avg_ms,
			MAX(duration_ms) AS slowest_ms,
			MIN(request_time) AS first_call,
			MAX(request_time) AS last_call
		FROM `tab{LOG}`
		WHERE {" AND ".join(conditions)}
		GROUP BY endpoint
		ORDER BY COUNT(*) DESC
		""",
		values,
		as_dict=True,
	)
	return _columns(), rows


def _columns():
	def col(fieldname, label, fieldtype, width):
		return {"fieldname": fieldname, "label": _(label), "fieldtype": fieldtype, "width": width}

	return [
		col("endpoint", "Endpoint", "Data", 230),
		col("calls", "Calls", "Int", 80),
		col("errors", "Errors", "Int", 80),
		col("rate_limited", "Rate limited", "Int", 110),
		col("bad_payload", "Bad payload", "Int", 110),
		col("auth", "Auth", "Int", 80),
		col("our_fault", "Our fault", "Int", 100),
		col("not_found", "Not found", "Int", 100),
		col("avg_ms", "Avg ms", "Int", 80),
		col("slowest_ms", "Slowest ms", "Int", 100),
		col("first_call", "First call", "Datetime", 170),
		col("last_call", "Last call", "Datetime", 170),
	]
