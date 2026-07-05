# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now


class CRMAPIRequestLog(Document):
	"""Raw request-level log. Pure data — written by observability.capture, read by
	observability.rollup. Kept light on the insert hot path.

	The Observability::Requests::logging toggle registers/deregisters this doctype with Log
	Settings (see capture.apply_logging). `clear_old_logs` (the LogType contract) is what makes
	Log Settings accept and KEEP that entry — without it the daily cleanup prunes any row it
	can't clear. The rollup reads these rows into CRM API Metric long before 90 days."""

	@staticmethod
	def clear_old_logs(days=90):
		table = frappe.qb.DocType("CRM API Request Log")
		frappe.db.delete(table, filters=(table.creation < (Now() - Interval(days=days))))
