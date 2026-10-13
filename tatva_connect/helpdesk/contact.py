"""The ONE contact brain: a Contact is the person, found by phone or email, grown by what it lacks, or created with both."""
import frappe
import phonenumbers
from frappe import _
from frappe.contacts.doctype.contact.contact import get_contact_name, get_contact_with_phone_number
from frappe.utils import parse_addr, validate_email_address

from tatva_connect.access import posture
from tatva_connect.helpdesk import SETTINGS
from tatva_connect.whatsapp.phone import to_e164


def _match(e164, email):
	"""(the Contact holding this phone or email, whether the phone found it) — phone first, it is the natural key."""
	by_phone = e164 and get_contact_with_phone_number(str(phonenumbers.parse(e164).national_number))
	return (by_phone or (email and get_contact_name(email)) or None), bool(by_phone)


def find_contact(number=None, email=None, fieldname="mobile_no"):
	"""The Contact already behind a phone and/or an email, or None; never writes."""
	return _match(to_e164(number, fieldname=fieldname) if number else "", email)[0]


def contact_of(value, create=False):
	"""The Contact a single value names: the Contact itself, or the one behind an email or a phone; made when neither matches only if `create`."""
	if not value or frappe.db.exists("Contact", value):
		return value
	return contact_for(email=value, create=create) if "@" in value else contact_for(number=value, create=create)


@frappe.whitelist(methods=["POST"])
def create_contact(doc: dict, invite: bool = False) -> str:
	"""Helpdesk's own create, asked first whether the person exists: a Contact behind this phone or email is reused and grown, never made twice."""
	from helpdesk.api.contact import create_contact as create_new

	frappe.has_permission("Contact", "create", throw=True)
	doc = frappe.parse_json(doc)
	full_name = " ".join(filter(None, (doc.get("first_name"), doc.get("last_name")))) or None
	# An invite goes to an email, so a contact made without one is never invited.
	return contact_for(doc.get("phone"), full_name, doc.get("email"), fieldname="phone", create=False) or create_new(doc, invite and bool(doc.get("email")))


def contact_for_sender(sender, create=False):
	"""The Contact behind an incoming mail's sender, made when new only if `create`; None for a sender HD Settings lists as never a contact."""
	full_name, email = parse_addr(sender or "")
	email = validate_email_address((email or "").strip().lower())
	if not email or email in _no_contact_emails():
		return None
	return contact_for(full_name=full_name or None, email=email, create=create)


def contact_for_lead(lead_name, create=False):
	"""The Contact a lead becomes: found by its phone or email, or made with them and the lead's name only if `create`."""
	if not lead_name:
		frappe.throw(_("A contact is made from a lead's phone or email, and this run has no lead."))
	lead = frappe.get_doc("CRM Lead", lead_name)
	return contact_for(lead.mobile_no, lead.lead_name, lead.email, create=create)


def _no_contact_emails():
	"""The senders HD Settings lists, one per line, lowercased."""
	listed = frappe.db.get_single_value(SETTINGS, "custom_no_contact_emails") or ""
	return {line.strip().lower() for line in listed.splitlines() if line.strip()}


def contact_for(number=None, full_name=None, email=None, fieldname="mobile_no", create=False):
	"""The Contact behind a phone and/or an email, with any phone, email or name it lacked added; a new one only when neither matches and the caller passes `create`."""
	e164 = to_e164(number, fieldname=fieldname) if number else ""
	name, by_phone = _match(e164, email)
	if not name and not (create and (e164 or email)):
		return None
	contact = frappe.get_doc("Contact", name) if name else frappe.new_doc("Contact")
	before = (len(contact.phone_nos), len(contact.email_ids), contact.first_name)
	if e164 and not by_phone:
		contact.add_phone(e164, is_primary_phone=not contact.phone, is_primary_mobile_no=not contact.mobile_no)
	if email:
		contact.add_email(email, is_primary=not contact.email_id)
	if full_name and not contact.first_name:
		contact.first_name = full_name
	if contact.is_new():
		contact.insert(ignore_permissions=posture.is_trusted())  # authz-ok: tier-b — the posture seam; an agent is ordinary, the partner and the intake form are pre-gated
	elif (len(contact.phone_nos), len(contact.email_ids), contact.first_name) != before:
		contact.save(ignore_permissions=posture.is_trusted())  # authz-ok: tier-b — the posture seam; only what the Contact lacked is added
	return contact.name
