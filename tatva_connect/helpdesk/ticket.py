"""HD Ticket override: a gated partner may raise a ticket for the contact it names; every other lane is stock helpdesk."""
import frappe
from frappe import _
from helpdesk.helpdesk.doctype.hd_ticket.hd_ticket import HDTicket

from tatva_connect.api._base import in_partner_lane, throw_by_audience
from tatva_connect.helpdesk import transitions

SUB_TYPE = "HD Ticket Sub Type"


class TatvaHDTicket(HDTicket):
	def validate(self):
		super().validate()
		self.validate_sub_type()
		transitions.guard(self)

	def validate_sub_type(self):
		# A sub type declares the type it belongs to; the picker filters on it and this is the same rule on the write.
		if not self.custom_ticket_sub_type or not self.has_value_changed("custom_ticket_sub_type"):
			return
		owner = frappe.db.get_value(SUB_TYPE, self.custom_ticket_sub_type, "ticket_type")
		if owner == self.ticket_type:
			return
		throw_by_audience(
			_("{0} belongs to the ticket type {1}. Set that type, or pick a sub type of {2}.").format(
				self.custom_ticket_sub_type, owner, self.ticket_type or _("this ticket's type")),
			_("`ticket_sub_type` reads `{0}`, which belongs to the ticket type `{1}`. Send that `ticket_type`, "
			  "or a sub type of `{2}`.").format(self.custom_ticket_sub_type, owner, self.ticket_type or ""),
			["ticket_sub_type", "ticket_type"],
		)

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
