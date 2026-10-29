# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One channel account: the team, source and grain a ticket arriving through it belongs to."""
import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.taxonomy import grain


class HDTicketRouting(Document):
	def validate(self):
		if not (self.agent_group or self.ticket_source or any(self.get(col) for col in grain.own_columns(self.doctype) if col)):
			frappe.throw(_("A routing row that names no team, source or grain does nothing."))
