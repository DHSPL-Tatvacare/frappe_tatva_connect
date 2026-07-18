# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CRMIntakeFieldMap(Document):
	def validate(self):
		table = (self.target_table or "").strip()
		if not table or table == "note":
			return  # blank (layout field) and note (free-text) are not sections
		if not frappe.db.exists("CRM Lead Section", table):
			frappe.throw(
				_("Target Table '{0}' is not a known CRM Lead Section (nor blank/note).").format(table),
				title=_("Unknown Target Table"),
			)
