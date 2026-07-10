# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now


class CRMFileScanLog(Document):
	"""Raw, disposable per-upload scan verdict for screened inbound files. Pure data — written
	out-of-band by storage.file_screening (an enqueued job, so a BLOCKED row survives the rollback
	that rejects the file). The Storage::File::screening activator (file_screening.apply_scan_logging)
	registers this doctype with Log Settings; `clear_old_logs` (the LogType contract) is what makes
	the daily cleanup accept and KEEP that entry."""

	@staticmethod
	def clear_old_logs(days=90):
		table = frappe.qb.DocType("CRM File Scan Log")
		frappe.db.delete(table, filters=(table.creation < (Now() - Interval(days=days))))
