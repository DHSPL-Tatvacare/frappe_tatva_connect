"""HD Ticket override: a gated partner may raise a ticket for the contact it names; every other lane is stock helpdesk."""
from helpdesk.helpdesk.doctype.hd_ticket.hd_ticket import HDTicket

from tatva_connect.api._base import in_partner_lane


class TatvaHDTicket(HDTicket):
	def validate_portal_contact(self):
		# The partner's contract + grain fence already authorised this contact; stock would refuse any non-agent naming one.
		if in_partner_lane():
			return
		super().validate_portal_contact()
