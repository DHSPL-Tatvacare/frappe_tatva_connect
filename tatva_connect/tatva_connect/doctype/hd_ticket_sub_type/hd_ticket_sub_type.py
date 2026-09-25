# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One level under helpdesk's Ticket Type: the sub type declares the type it belongs to, and a ticket may pair it with no other."""
from frappe.model.document import Document


class HDTicketSubType(Document):
	pass
