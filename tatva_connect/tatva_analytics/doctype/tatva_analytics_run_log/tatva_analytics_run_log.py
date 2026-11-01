import frappe
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now


class TatvaAnalyticsRunLog(Document):
	@staticmethod
	def clear_old_logs(days=30):
		table = frappe.qb.DocType("Tatva Analytics Run Log")
		frappe.db.delete(table, filters=(table.creation < (Now() - Interval(days=days))))
