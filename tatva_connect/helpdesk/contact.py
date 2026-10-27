"""The ONE contact brain: a Contact is the person, found by phone or email, grown by what it lacks, or created with both."""
import frappe
import phonenumbers
from frappe import _
from frappe.contacts.doctype.contact.contact import get_contact_name, get_contact_with_phone_number
from frappe.utils import parse_addr, validate_email_address

from tatva_connect.access import posture
from tatva_connect.helpdesk import SETTINGS
from tatva_connect.whatsapp.phone import lookup_e164, to_e164


def _match(e164, email):
	"""(the Contact holding this phone or email, whether the phone found it) — phone first, it is the natural key."""
	by_phone = e164 and get_contact_with_phone_number(str(phonenumbers.parse(e164).national_number))
	return (by_phone or (email and get_contact_name(email)) or None), bool(by_phone)


def find_contact(number=None, email=None):
	"""The Contact already behind a phone and/or an email, or None; never writes, and a malformed number finds nothing."""
	return _match(lookup_e164(number) if number else "", email)[0]


def _one_value(value):
	"""(number, email) a single value stands for: an email when it holds an @, else a phone."""
	return (None, value) if "@" in value else (value, None)


def find_contact_of(value):
	"""The Contact a single value names, never writing: the Contact itself, or the one behind an email or a phone."""
	if not value or frappe.db.exists("Contact", value):
		return value or None
	number, email = _one_value(value)
	return find_contact(number=number, email=email)


def contact_of(value, create=False):
	"""The Contact a single value names: the Contact itself, or the one behind an email or a phone; made when neither matches only if `create`."""
	if not value or frappe.db.exists("Contact", value):
		return value
	number, email = _one_value(value)
	# A person known only by a phone is named by it, so a new contact never lands with a blank name and a random ID.
	return contact_for(number=number, full_name=number, email=email, create=create)


@frappe.whitelist(methods=["POST"])
def create_contact(doc: dict, invite: bool = False, ask_if_exists: bool = False, update_existing: bool = True) -> str | dict:
	"""Helpdesk's own create, asked first whether the person exists; `ask_if_exists` answers the match instead of reusing it."""
	from helpdesk.api.contact import create_contact as create_new

	frappe.has_permission("Contact", "create", throw=True)
	doc = frappe.parse_json(doc)
	# Stock demands a `+country code`; the typed number is read as the stored form first, so `98765…` is enough.
	doc["phone"] = to_e164(doc.get("phone"), fieldname="phone")
	holder = _holder([doc.get("phone")], [doc.get("email")])
	if holder and ask_if_exists:
		return {"existing": holder, "adds": _adds(holder, doc)}
	if holder and not update_existing:
		return holder
	full_name = " ".join(filter(None, (doc.get("first_name"), doc.get("last_name")))) or None
	# An invite goes to an email, so a contact made without one is never invited.
	return contact_for(doc.get("phone"), full_name, doc.get("email"), fieldname="phone", create=False) or create_new(doc, invite and bool(doc.get("email")))


def _adds(name, doc):
	"""Would the typed phone or email give this contact one it lacks; what `contact_for` adds on a reuse."""
	contact = frappe.get_doc("Contact", name)
	email = (doc.get("email") or "").strip().lower()
	return bool((email and email not in {e.email_id.lower() for e in contact.email_ids})
	            or (doc.get("phone") and doc["phone"] not in {lookup_e164(p.phone) for p in contact.phone_nos}))


@frappe.whitelist()
def existing_holder(phones: list | str | None = None, emails: list | str | None = None, exclude: str | None = None) -> str | None:
	"""Another contact this user may read that already holds one of these phones or emails, so a form can stop a duplicate."""
	frappe.has_permission("Contact", "read", throw=True)
	return _holder(frappe.parse_json(phones) or [], frappe.parse_json(emails) or [], exclude)


def _holder(phones, emails, exclude=None):
	"""The one answer to "who already holds this": a contact other than `exclude` this user may read, or None."""
	if exclude and frappe.db.exists("Contact", exclude) and frappe.has_permission("Contact", "read", doc=exclude):
		phones, emails = _not_yet_held(exclude, phones, emails)  # an edit is checked for what it adds; what the contact had never blocks its own save
	found = [find_contact(number=p) for p in phones if p] + [find_contact(email=e) for e in emails if e]
	return next((c for c in found if c and c != exclude and frappe.has_permission("Contact", "read", doc=c)), None)


def _not_yet_held(name, phones, emails):
	"""Of these phones and emails, those the saved contact `name` does not already hold."""
	contact = frappe.get_doc("Contact", name)
	had_phones = {lookup_e164(p.phone) for p in contact.phone_nos if p.phone}
	had_emails = {e.email_id.strip().lower() for e in contact.email_ids if e.email_id}
	return ([p for p in phones if p and lookup_e164(p) not in had_phones],
	        [e for e in emails if e and e.strip().lower() not in had_emails])


def refuse_inbox_contacts(doc, method=None):
	"""Email Account validate: turning on the inbox's own Create Contact is refused; it makes a contact for every address on a mail."""
	if doc.create_contact and (doc.is_new() or doc.has_value_changed("create_contact")):
		frappe.throw(_("Tatva Connect keeps this off, because it makes a contact for every address on a mail. "
		               "Contacts from email are made in HD Settings, on the Tatva Connect tab: Create a contact for a new email sender."),
		             title=_("Create Contact stays off"))


@frappe.whitelist(methods=["POST"])
def edit_contact(name: str, doc: dict) -> str:
	"""Helpdesk's own edit, each typed phone read as the stored form first, so a number needs no `+country code`."""
	from helpdesk.api.contact import edit_contact as edit

	doc = frappe.parse_json(doc)
	for row in doc.get("phone_nos") or []:
		if row.get("phone"):
			row["phone"] = to_e164(row["phone"], fieldname="phone")
	# An edit may not give this contact a phone or email another contact already holds, as Desk's form refuses too.
	holder = _holder([r.get("phone") for r in doc.get("phone_nos") or []], [r.get("email_id") for r in doc.get("email_ids") or []], name)
	if holder:
		frappe.throw(_("{0} already has this phone or email. Open that contact, or merge the two in Desk.").format(holder),
		             title=_("Contact already exists"))
	return edit(name, doc)


@frappe.whitelist()
def get_quick_filters(doctype: str, show_customer_portal_fields: bool = False) -> list:
	"""Helpdesk's quick filters, plus name, mobile and email on the contacts list, where stock offers the ID alone."""
	from helpdesk.api.doc import get_quick_filters as stock

	filters = stock(doctype, show_customer_portal_fields)
	if doctype == "Contact":
		meta = frappe.get_meta(doctype)
		# The title and the search fields the Contact meta declares, so the filters follow the Property Setter.
		fields = [meta.title_field, *(f.strip() for f in (meta.search_fields or "").split(",") if f.strip())]
		filters += [{"label": _(meta.get_label(f)), "name": f, "type": meta.get_field(f).fieldtype} for f in fields]
	return filters


def canonical_phones(doc, method=None):
	"""before_validate on Contact: a phone typed or changed is stored E.164; a saved row is left as it is, and a staff contact is stock."""
	if doc.user:
		return
	before = doc.get_doc_before_save()
	saved = {row.phone for row in before.phone_nos} if before else set()
	for row in doc.phone_nos:
		if row.phone and row.phone not in saved:
			row.phone = to_e164(row.phone, fieldname="phone")


class ConvertFindsContact:
	"""Mixed into CRM Lead: Convert to Deal finds the person the way every other path does, not by exact phone text."""

	def contact_exists(self, throw=True):
		contact = find_contact(self.mobile_no, self.email) or find_contact(self.phone)
		if contact and throw:
			frappe.throw(_("Contact already exists: {0}").format(contact), title=_("Contact Already Exists"))
		return contact or False


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
