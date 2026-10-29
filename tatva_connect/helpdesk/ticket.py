"""HD Ticket override: a gated partner may raise a ticket for the contact it names, and helpdesk's own reply-driven reopen is not judged as an agent's move; every other lane is stock helpdesk."""
import contextlib

import frappe
from frappe import _
from helpdesk.helpdesk.doctype.hd_ticket.hd_ticket import HDTicket

from tatva_connect.access import posture
from tatva_connect.api._base import in_partner_lane
from tatva_connect.helpdesk import SETTINGS, TICKET, classification, contact, routing, transitions
from tatva_connect.helpdesk.transitions import CUSTOMER_REPLY, label_of

# The fields we added to the ticket; helpdesk's timeline shows the five it shipped with and no more.
TIMELINE_FIELDS = ("custom_ticket_source", "custom_ticket_sub_type", "custom_ticket_bucket",
                   "custom_internal_team", "custom_resolution_reason")


def _gated_lane():
	"""A server lane that already authorised its caller: the partner API, or a published intake form's fold."""
	return in_partner_lane() or posture.is_trusted()


class TatvaHDTicket(HDTicket):
	def before_validate(self):
		super().before_validate()
		stamp_agent_grain(self)

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

	def before_save(self):
		# A change to our fields reads on the timeline as an Info comment, as helpdesk's own tag changes do.
		super().before_save()
		if self.is_new():
			return
		for fieldname in TIMELINE_FIELDS:
			if not self.has_value_changed(fieldname):
				continue
			spoken = label_of(fieldname).lower()
			value = self.get(fieldname)
			self.add_comment("Info", _("set {0} to {1}").format(spoken, value) if value
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
		# Staff who raise a ticket for a contact name the contact, by their email or none; a partner keeps the documented key user.
		if (self.contact and (self.is_new() or self.has_value_changed("contact"))
		        and self.raised_by in (None, "", frappe.session.user) and not in_partner_lane()):
			self.raised_by = frappe.db.get_value("Contact", self.contact, "email_id") or ""
			return
		super().set_raised_by()

	def set_contact(self):
		super().set_contact()
		# Mail that opens a ticket links its sender's Contact, made only when HD Settings says so; the inbox's own tick would also make one per address on the mail.
		if not self.contact and self.is_new() and self.email_account:
			create = bool(frappe.db.get_single_value(SETTINGS, "custom_create_contact_from_email"))
			self.contact = contact.contact_for_sender(self.raised_by, create=create)

	def send_acknowledgement_email(self):
		# No requester email, no one to acknowledge; a source that mutes automatic email sends none either.
		if self.raised_by and not self._source_mutes_email():
			super().send_acknowledgement_email()

	def handle_email_feedback(self):
		if not self._source_mutes_email():
			super().handle_email_feedback()

	def _source_mutes_email(self):
		return bool(self.custom_ticket_source and frappe.db.get_value("HD Ticket Source", self.custom_ticket_source, "no_automatic_email"))

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


def stamp_agent_grain(doc):
	"""A new ticket an agent raises takes their grain through the lead's own stamp; mail, forms, workflows and partners bring theirs."""
	if not doc.is_new() or doc.flags.ignore_permissions:
		return
	if frappe.get_cached_value("User", frappe.session.user, "user_type") != "System User":
		return  # a portal customer holds no grain, and the portal is where their tickets come from
	from tatva_connect.lead.leads import stamp_creator_grain

	stamp_creator_grain(doc, _("ticket"))


@frappe.whitelist(methods=["GET"])
def grain_choices():
	"""The new-ticket form's Grain field: every exact grain the caller can file a ticket under, and the ticket's grain columns."""
	from tatva_connect.access import entitlement
	from tatva_connect.taxonomy import grain

	leaves = entitlement.filing_leaves()
	return {"grains": [dict(zip(grain.AXES, g, strict=True)) for g in leaves], "fields": grain.own_columns(TICKET)}
