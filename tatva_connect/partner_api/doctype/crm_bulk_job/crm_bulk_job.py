# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CRMBulkJob(Document):
	"""One async partner bulk-create job. The submitted batch (inline JSON or an uploaded CSV/JSONL
	file, stored as this record's own attachment) is drained on the partner_bulk worker through the
	same create brain the sync endpoints use. States follow Salesforce Bulk API 2.0."""

	def on_trash(self):
		"""Cascade the standalone result rows; the attached file is removed by Frappe's own cleanup."""
		frappe.db.delete("CRM Bulk Job Result", {"job": self.name})
