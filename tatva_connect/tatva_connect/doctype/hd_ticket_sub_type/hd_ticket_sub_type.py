# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One level under helpdesk's Ticket Type: the sub type declares the type it belongs to, and a ticket may pair it with no other."""
from frappe.model.document import Document

from tatva_connect.helpdesk.classification import rebuild


class HDTicketSubType(Document):
	def on_update(self):
		rebuild()

	def on_trash(self):
		rebuild()
