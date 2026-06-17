# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMTatvaAutomation(Document):
	def validate(self):
		# Guards + infra always run — they are never operator-togglable, so the row
		# is forced enabled (the form keeps `enabled` read-only for these kinds).
		if self.kind in ("Guard", "Infra"):
			self.enabled = 1
