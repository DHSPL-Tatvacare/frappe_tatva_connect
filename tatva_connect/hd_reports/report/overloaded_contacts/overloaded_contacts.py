# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Contacts holding more than one email or phone, so a contact carrying several people's details shows at a glance."""
import frappe
from frappe import _
from frappe.utils import cint

from tatva_connect.hd_reports.contact_hygiene import CONTACT, email_key, phone_key, readable_rows

# Each kind: the Contact child table field, the column holding the value, and its comparison key.
KINDS = {"Email": ("email_ids", "email_id", email_key), "Phone": ("phone_nos", "phone", phone_key)}


def contact_values(kind):
	"""Every readable contact's values of one kind as {contact: {key: raw}}, one entry per distinct key."""
	table, column, key_of = KINDS[kind]
	held = {}
	for r in readable_rows(frappe.get_meta(CONTACT).get_field(table).options, [column]):
		if key := key_of(r[column]):
			held.setdefault(r.parent, {}).setdefault(key, r[column])
	return held


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(CONTACT, "read", throw=True)
	floor, which = max(cint(filters.min_count) or 2, 1), filters.which or "Either"
	emails, phones = contact_values("Email"), contact_values("Phone")
	over = {c for c, v in emails.items() if len(v) >= floor} if which in ("Emails", "Either") else set()
	if which in ("Phones", "Either"):
		over |= {c for c, v in phones.items() if len(v) >= floor}
	if not over:
		return _columns(), []
	heads = frappe.get_list(CONTACT, filters={"name": ["in", list(over)]}, fields=["name", "full_name", "email_id", "mobile_no", "phone"],
		limit_page_length=0)
	links = {}
	for link in frappe.get_list(CONTACT, filters={"name": ["in", list(over)]}, fields=["name", "links.link_doctype as dt", "links.link_name as dn"],
		limit_page_length=0):
		if link.dn:
			links.setdefault(link.name, []).append(f"{link.dt}: {link.dn}")
	rows = [{
		"contact": h.name, "full_name": h.full_name,
		"email_count": len(emails.get(h.name, {})), "phone_count": len(phones.get(h.name, {})),
		"emails": ", ".join(emails.get(h.name, {}).values()), "phones": ", ".join(phones.get(h.name, {}).values()),
		"primary_email": h.email_id, "primary_phone": h.mobile_no or h.phone, "links": ", ".join(links.get(h.name, [])),
	} for h in heads]
	rows.sort(key=lambda r: (-max(r["email_count"], r["phone_count"]), r["contact"]))
	return _columns(), rows


def _columns():
	return [
		{"label": _("Contact"), "fieldname": "contact", "fieldtype": "Link", "options": CONTACT, "width": 200},
		{"label": _("Full Name"), "fieldname": "full_name", "fieldtype": "Data", "width": 180},
		{"label": _("Emails #"), "fieldname": "email_count", "fieldtype": "Int", "width": 90},
		{"label": _("Phones #"), "fieldname": "phone_count", "fieldtype": "Int", "width": 90},
		{"label": _("Emails"), "fieldname": "emails", "fieldtype": "Data", "width": 280},
		{"label": _("Phones"), "fieldname": "phones", "fieldtype": "Data", "width": 220},
		{"label": _("Primary Email"), "fieldname": "primary_email", "fieldtype": "Data", "width": 200},
		{"label": _("Primary Phone"), "fieldname": "primary_phone", "fieldtype": "Data", "width": 150},
		{"label": _("Linked Records"), "fieldname": "links", "fieldtype": "Data", "width": 280},
	]
