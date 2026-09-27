"""HD Ticket override: a gated partner may raise a ticket for the contact it names, and helpdesk's own reply-driven reopen is not judged as an agent's move; every other lane is stock helpdesk."""
import contextlib

import frappe
from helpdesk.helpdesk.doctype.hd_ticket.hd_ticket import HDTicket

from tatva_connect.api._base import in_partner_lane
from tatva_connect.helpdesk import classification, transitions
from tatva_connect.helpdesk.transitions import CUSTOMER_REPLY


class TatvaHDTicket(HDTicket):
	def validate(self):
		super().validate()
		classification.validate_pair(self)
		transitions.guard(self)

	@contextlib.contextmanager
	def replying_customer(self):
		"""Helpdesk reopens a ticket itself when mail arrives; the rulebook judges what an agent chooses, not that."""
		self.flags[CUSTOMER_REPLY] = True
		try:
			yield
		finally:
			self.flags[CUSTOMER_REPLY] = False

	def create_communication_via_contact(self, *args, **kwargs):
		with self.replying_customer():
			return super().create_communication_via_contact(*args, **kwargs)

	def on_communication_update(self, c):
		with self.replying_customer():
			return super().on_communication_update(c)

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
