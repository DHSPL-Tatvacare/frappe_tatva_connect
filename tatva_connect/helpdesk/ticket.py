"""HD Ticket override: a gated partner may raise a ticket for the contact it names; every other lane is stock helpdesk."""
import frappe
from helpdesk.helpdesk.doctype.hd_ticket.hd_ticket import HDTicket

from tatva_connect.api._base import in_partner_lane


class TatvaHDTicket(HDTicket):
	def validate_portal_contact(self):
		# The partner's contract + grain fence already authorised this contact; stock would refuse any non-agent naming one.
		if in_partner_lane():
			return
		super().validate_portal_contact()

	def tag_first_ticket(self):
		# Stock's tag write re-checks HD Ticket write on a fresh doc, which a pre-gated partner never holds; stock runs as Administrator for this step only.
		if not in_partner_lane():
			return super().tag_first_ticket()
		current_user, form_dict = frappe.session.user, frappe.local.form_dict
		frappe.set_user("Administrator")
		try:
			super().tag_first_ticket()
		finally:
			frappe.set_user(current_user)
			frappe.local.form_dict = form_dict  # set_user blanks the request body; the partner request still owns it
