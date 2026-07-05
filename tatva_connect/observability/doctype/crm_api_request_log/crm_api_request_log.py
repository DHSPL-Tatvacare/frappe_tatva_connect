# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now


class CRMAPIRequestLog(Document):
	"""Raw request-level log. Pure data — written by observability.capture, read by
	observability.rollup. Kept light on the insert hot path.

	Retention is owned by Frappe's Log Settings: this doctype is registered at 90 days via
	the `default_log_clearing_doctypes` hook, and the daily cleanup job calls `clear_old_logs`
	below. Implementing it (the `LogType` contract) is what makes Log Settings accept and KEEP
	the entry — without it, Log Settings prunes any row it can't clear. The rollup reads these
	rows into CRM API Metric long before 90 days, so trimming never loses aggregated history."""

	@staticmethod
	def clear_old_logs(days=90):
		table = frappe.qb.DocType("CRM API Request Log")
		frappe.db.delete(table, filters=(table.creation < (Now() - Interval(days=days))))
