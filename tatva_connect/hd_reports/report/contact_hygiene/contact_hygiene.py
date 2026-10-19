# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One row per contact-data problem with its count, so the state of the contact book reads at a glance."""
from collections import defaultdict

import frappe
from frappe import _

from tatva_connect.hd_reports.contact_hygiene import (
	CONTACT,
	dummy_email,
	email_key,
	parse_patterns,
	phone_key,
	readable_rows,
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(CONTACT, "read", throw=True)
	patterns = parse_patterns(filters.patterns)
	emails = _keys_by_contact(readable_rows("Contact Email", ["email_id"]), "email_id", email_key)
	phones = _keys_by_contact(readable_rows("Contact Phone", ["phone"]), "phone", phone_key)
	rows = [
		_row(_("Contacts with a dummy email"), sum(any(dummy_email(e, patterns) for e in keys) for keys in emails.values()),
			_("At least one email matches a dummy pattern.")),
		_row(_("Contacts with no email and no phone"), _no_email_no_phone(),
			_("Nothing to reach the contact by.")),
		_row(_("Contacts holding more than one email"), sum(len(keys) > 1 for keys in emails.values()),
			_("Several distinct email addresses on one contact.")),
		_row(_("Contacts holding more than one phone"), sum(len(keys) > 1 for keys in phones.values()),
			_("Several distinct phone numbers on one contact, compared on the last ten digits.")),
		_row(_("Emails on more than one contact"), _shared(emails),
			_("One email address appears on several contacts.")),
		_row(_("Phones on more than one contact"), _shared(phones),
			_("One phone number appears on several contacts, compared on the last ten digits.")),
	]
	columns = [
		{"label": _("Problem"), "fieldname": "problem", "fieldtype": "Data", "width": 300},
		{"label": _("Count"), "fieldname": "count", "fieldtype": "Int", "width": 100},
		{"label": _("What it means"), "fieldname": "description", "fieldtype": "Data", "width": 460},
	]
	chart = {"type": "bar", "data": {"labels": [r["problem"] for r in rows], "datasets": [{"name": _("Count"), "values": [r["count"] for r in rows]}]}}
	return columns, rows, None, chart


def _row(problem, count, description):
	return {"problem": problem, "count": count, "description": description}


def _keys_by_contact(rows, field, key):
	"""Each readable contact's distinct non-blank keys for one child field."""
	keys = defaultdict(set)
	for r in rows:
		if k := key(r[field]):
			keys[r.parent].add(k)
	return keys


def _shared(keys_by_contact):
	"""How many keys sit on more than one contact."""
	holders = defaultdict(int)
	for keys in keys_by_contact.values():
		for k in keys:
			holders[k] += 1
	return sum(n > 1 for n in holders.values())


def _no_email_no_phone():
	"""Readable contacts with no Contact Email row and no Contact Phone row."""
	contact, email, phone = frappe.qb.DocType(CONTACT), frappe.qb.DocType("Contact Email"), frappe.qb.DocType("Contact Phone")
	query = frappe.qb.get_query(CONTACT, fields=[{"COUNT": "*", "as": "n"}], ignore_permissions=False)
	query = query.where(contact.name.notin(frappe.qb.from_(email).select(email.parent).where(email.parenttype == CONTACT)))
	query = query.where(contact.name.notin(frappe.qb.from_(phone).select(phone.parent).where(phone.parenttype == CONTACT)))
	return query.run(as_dict=True)[0].n
