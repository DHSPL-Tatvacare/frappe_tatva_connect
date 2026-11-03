# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The bucket a ticket is filed under, a curated master beside Type, Sub Type and Source."""
from frappe.model.document import Document

from tatva_connect.helpdesk.classification import refuse_twin


class HDTicketBucket(Document):
	def validate(self):
		refuse_twin(self)
