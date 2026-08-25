# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One screening answer. The row derives its own identity, so no writer can store one that disagrees."""

from frappe.model.document import Document

from tatva_connect.lead import keyvalue


class CRMLeadScreeningAnswer(Document):
	def validate(self):
		# The guard for a row saved on its own: frappe runs `validate` on the parent, so a row written through a lead is stamped by the write engine (`api/partner.py::_apply_key_value`) by the same rule.
		self.question_hash = keyvalue.identity_of(self.question)
