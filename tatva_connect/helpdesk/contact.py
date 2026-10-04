"""The ONE contact brain for helpdesk: a Contact is the person, found by phone or email, grown by what it lacks, or created with both."""
import frappe
import phonenumbers
from frappe.contacts.doctype.contact.contact import get_contact_name, get_contact_with_phone_number

from tatva_connect.access import posture
from tatva_connect.whatsapp.phone import to_e164


def _match(e164, email):
	"""(the Contact holding this phone or email, whether the phone found it) — phone first, it is the natural key."""
	by_phone = e164 and get_contact_with_phone_number(str(phonenumbers.parse(e164).national_number))
	return (by_phone or (email and get_contact_name(email)) or None), bool(by_phone)


def find_contact(number=None, email=None, fieldname="mobile_no"):
	"""The Contact already behind a phone and/or an email, or None; never writes."""
	return _match(to_e164(number, fieldname=fieldname) if number else "", email)[0]


def contact_for(number=None, full_name=None, email=None, fieldname="mobile_no", create=True):
	"""The Contact behind a phone and/or an email, with any phone, email or name it lacked added; a new one when neither matches and `create` allows, else None."""
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
