# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One email or phone held by more than one contact, the usual sign of a duplicate contact."""
import frappe
from frappe import _

from tatva_connect.hd_reports.contact_hygiene import CONTACT
from tatva_connect.hd_reports.report.overloaded_contacts.overloaded_contacts import (
	KINDS,
	contact_values,
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(CONTACT, "read", throw=True)
	rows = []
	for kind in [filters.kind] if filters.kind else list(KINDS):
		holders = {}
		for contact, values in contact_values(kind).items():
			for key, raw in values.items():
				holders.setdefault(key, []).append((contact, raw))
		holders = {k: h for k, h in holders.items() if len(h) > 1}
		born = _creation({c for h in holders.values() for c, _raw in h})
		for held in holders.values():
			held.sort(key=lambda h: (born.get(h[0]), h[0]))
		rows += [{
			"value": held[0][1], "kind": kind, "contacts_count": len(held),
			"contacts": ", ".join(c for c, _raw in held), "oldest_contact": held[0][0],
		} for held in holders.values()]
	rows.sort(key=lambda r: (-r["contacts_count"], r["kind"], r["value"]))
	return _columns(), rows


def _creation(contacts):
	"""Each contact's creation time, read through the Contact list so the oldest is picked among readable ones."""
	if not contacts:
		return {}
	return dict(frappe.get_list(CONTACT, filters={"name": ["in", list(contacts)]}, fields=["name", "creation"], limit_page_length=0, as_list=True))


def _columns():
	return [
		{"label": _("Value"), "fieldname": "value", "fieldtype": "Data", "width": 240},
		{"label": _("Kind"), "fieldname": "kind", "fieldtype": "Data", "width": 80},
		{"label": _("Contacts #"), "fieldname": "contacts_count", "fieldtype": "Int", "width": 100},
		{"label": _("Contacts"), "fieldname": "contacts", "fieldtype": "Data", "width": 360},
		{"label": _("Oldest Contact"), "fieldname": "oldest_contact", "fieldtype": "Link", "options": CONTACT, "width": 200},
	]
