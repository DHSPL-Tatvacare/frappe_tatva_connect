# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Why a ticket was resolved, picked when an agent resolves it."""
from frappe.model.document import Document

from tatva_connect.helpdesk.classification import refuse_twin


class HDTicketResolutionReason(Document):
	def validate(self):
		refuse_twin(self)
