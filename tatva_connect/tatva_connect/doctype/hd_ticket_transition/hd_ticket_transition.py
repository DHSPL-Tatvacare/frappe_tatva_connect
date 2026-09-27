# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One allowed move of a ticket: from a status, to a status, by a role, once the fields it names are filled."""
import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.helpdesk import TICKET


class HDTicketTransition(Document):
	def validate(self):
		self.validate_moves_somewhere()
		self.validate_fields_exist()

	def validate_moves_somewhere(self):
		if self.from_status == self.to_status:
			frappe.throw(_("A move goes from one status to another; {0} is both.").format(self.from_status))

	def validate_fields_exist(self):
		# The demanded field is asked of the ticket's own meta, so a renamed or mistyped column is refused here rather than silently never enforced.
		meta = frappe.get_meta(TICKET)
		for row in self.required_fields:
			if not meta.get_field(row.fieldname):
				frappe.throw(_("Row {0}: {1} is not a field of {2}.").format(row.idx, row.fieldname, TICKET))
