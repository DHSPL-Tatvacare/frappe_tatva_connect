"""The ONE contact-by-phone brain for helpdesk: a Contact is the person, found by number or created with it."""
import frappe
import phonenumbers
from frappe.contacts.doctype.contact.contact import get_contact_with_phone_number

from tatva_connect.access import posture
from tatva_connect.whatsapp.phone import to_e164


def contact_for_phone(number, full_name=None, email=None, fieldname="mobile_no"):
	"""The Contact holding `number` in any stored spelling that ends in its national digits, else a new one in E.164; the name may stay blank."""
	e164 = to_e164(number, fieldname=fieldname)
	found = get_contact_with_phone_number(str(phonenumbers.parse(e164).national_number))
	if found:
		return found
	contact = frappe.new_doc("Contact")
	contact.first_name = full_name
	contact.append("phone_nos", {"phone": e164, "is_primary_phone": 1, "is_primary_mobile_no": 1})
	if email:
		contact.append("email_ids", {"email_id": email, "is_primary": 1})
	contact.insert(ignore_permissions=posture.is_trusted())  # authz-ok: tier-b — the posture seam; an agent is ordinary, the partner is pre-gated by mapping + grain
	return contact.name
