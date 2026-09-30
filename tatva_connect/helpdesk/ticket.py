"""HD Ticket override: a gated partner may raise a ticket for the contact it names, and helpdesk's own reply-driven reopen is not judged as an agent's move; every other lane is stock helpdesk."""
import contextlib

import frappe
from frappe import _
from helpdesk.helpdesk.doctype.hd_ticket.hd_ticket import HDTicket
from helpdesk.helpdesk.doctype.hd_ticket_activity.hd_ticket_activity import log_ticket_activity

from tatva_connect.access import posture
from tatva_connect.api._base import in_partner_lane
from tatva_connect.helpdesk import classification, routing, transitions
from tatva_connect.helpdesk.transitions import CUSTOMER_REPLY, label_of

# The fields we added to the ticket; helpdesk's own logger knows the six it shipped with and no more.
TIMELINE_FIELDS = ("custom_ticket_source", "custom_ticket_sub_type",
                   "custom_internal_team", "custom_resolution_reason")


def _gated_lane():
	"""A server lane that already authorised its caller: the partner API, or a published intake form's fold."""
	return in_partner_lane() or posture.is_trusted()


class TatvaHDTicket(HDTicket):
	def validate(self):
		super().validate()
		routing.stamp(self)
		classification.validate_pair(self)
		classification.apply_priority(self)
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
		# Stock re-saves the ticket with the caller's permissions, which a gated lane never holds.
		self.flags.ignore_permissions = self.flags.ignore_permissions or _gated_lane()
		with self.replying_customer():
			return super().on_communication_update(c)

	def handle_ticket_activity_update(self):
		# A change to our fields reads on the timeline exactly as a change to helpdesk's own does.
		super().handle_ticket_activity_update()
		for fieldname in TIMELINE_FIELDS:
			if not self.has_value_changed(fieldname):
				continue
			spoken = label_of(fieldname).lower()
			value = self.get(fieldname)
			log_ticket_activity(self.name, _("set {0} to {1}").format(spoken, value) if value
			                    else _("cleared {0}").format(spoken))

	@staticmethod
	def filter_standard_fields(fields):
		# A filter over a master the customer may not read answers with a permission error, so the portal is not offered it.
		fields = HDTicket.filter_standard_fields(fields)
		return [f for f in fields
		        if f.get("type") != "Link" or frappe.has_permission(f.get("options"), "read")]

	def validate_portal_contact(self):
		# The partner's contract + grain fence, or a published intake form, already authorised this contact; stock would refuse any non-agent naming one.
		if _gated_lane():
			return
		super().validate_portal_contact()

	def set_raised_by(self):
		# The intake fold runs as the visitor, who is not the requester: with no email known the ticket names none rather than "Guest".
		if posture.is_trusted():
			return
		super().set_raised_by()

	def send_acknowledgement_email(self):
		# No requester email, no one to acknowledge.
		if self.raised_by:
			super().send_acknowledgement_email()

	def tag_first_ticket(self):
		# Stock's tag write re-checks HD Ticket write on a fresh doc, which a pre-gated partner or intake Guest never holds; stock runs as Administrator for this step only.
		if not _gated_lane():
			return super().tag_first_ticket()
		current_user, form_dict = frappe.session.user, frappe.local.form_dict
		frappe.set_user("Administrator")
		try:
			super().tag_first_ticket()
		finally:
			frappe.set_user(current_user)
			frappe.local.form_dict = form_dict  # set_user blanks the request body; the partner request still owns it
