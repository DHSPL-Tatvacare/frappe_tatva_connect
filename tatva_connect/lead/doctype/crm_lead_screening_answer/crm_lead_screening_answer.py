# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One screening answer. The row derives its own identity, so no writer can store one that disagrees."""

from frappe.model.document import Document

from tatva_connect.lead import keyvalue


class CRMLeadScreeningAnswer(Document):
	def validate(self):
		# Derived here rather than by the caller: the identity is a pure function of the question, and a
		# writer that computed it separately could store a row answering under a question it does not ask.
		self.question_hash = keyvalue.identity_of(self.question)
