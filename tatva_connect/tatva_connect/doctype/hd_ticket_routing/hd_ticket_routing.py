# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One channel account, one team: what a ticket arriving through this account belongs to."""
import frappe
from frappe import _
from frappe.model.document import Document


class HDTicketRouting(Document):
	def validate(self):
		if not self.agent_group and not self.ticket_source:
			frappe.throw(_("A routing row that names neither a team nor a source does nothing."))
