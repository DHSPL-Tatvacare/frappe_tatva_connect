# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMTatvaAutomation(Document):
	def validate(self):
		# Always-on rows always run — they are never operator-togglable, so the row
		# is forced enabled (the form keeps `enabled` read-only for these).
		if self.control == "Always-on":
			self.enabled = 1
