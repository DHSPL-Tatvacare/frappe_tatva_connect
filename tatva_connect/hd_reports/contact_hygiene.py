# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The one rule every HD contact report shares: what counts as a dummy email, and how an email or phone is keyed for comparison."""
from fnmatch import fnmatchcase

import frappe
from frappe.utils import cstr

from tatva_connect.phone import match_digits

CONTACT = "Contact"


def default_patterns() -> list[str]:
	"""The patterns used when the report filter is blank."""
	# Glob patterns on the whole lowercased address (* is any text): placeholder words, throwaway local parts, reserved or disposable domains.
	return [
		"*@example.com", "*@test.com", "*@mailinator.com",
		"na@*", "abc@*", "xyz@*", "123@*",
		"*test*", "*dummy*", "*example*", "*noreply*", "*no-reply*", "*sample*",
	]


def parse_patterns(text) -> list[str]:
	"""A filter's one-pattern-per-line text as a list; blank gives default_patterns()."""
	return [p.strip().lower() for p in cstr(text).splitlines() if p.strip()] or default_patterns()


def matched_pattern(email, patterns) -> str | None:
	"""The first pattern the email matches, or None."""
	key = email_key(email)
	return next((p for p in patterns if key and fnmatchcase(key, p.lower())), None)


def dummy_email(email, patterns) -> bool:
	"""True when the email matches any of the patterns."""
	return matched_pattern(email, patterns) is not None


def email_key(email) -> str:
	"""An email as compared across contacts: trimmed and lowercased."""
	return cstr(email).strip().lower()


def phone_key(phone) -> str:
	"""A phone as compared across contacts: its last ten digits, blank when shorter."""
	return match_digits(phone, last=10)


def readable_rows(child, fields) -> list[dict]:
	"""Rows of a Contact child table, only on contacts the viewer can read in the Contact list."""
	readable = frappe.qb.get_query(CONTACT, fields=["name"], ignore_permissions=False)
	query = frappe.qb.get_query(child, filters={"parenttype": CONTACT}, fields=["parent", *fields])  # authz-ok: tier-b — scoped to the Contact list's own permission query on the next line
	return query.where(frappe.qb.DocType(child).parent.isin(readable)).run(as_dict=True)
