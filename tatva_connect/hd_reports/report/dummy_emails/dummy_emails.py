# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Every email on a readable contact that matches a dummy pattern, with the pattern that caught it."""
import frappe
from frappe import _

from tatva_connect.hd_reports.contact_hygiene import CONTACT, matched_pattern, parse_patterns, readable_rows


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(CONTACT, "read", throw=True)
	patterns = parse_patterns(filters.patterns)
	hits = []
	for r in readable_rows("Contact Email", ["email_id", "is_primary"]):
		if pattern := matched_pattern(r.email_id, patterns):
			hits.append({"contact": r.parent, "email": r.email_id, "pattern": pattern, "is_primary": r.is_primary})
	contacts = {c.name: c for c in frappe.get_list(CONTACT, filters={"name": ["in", {h["contact"] for h in hits}]},
		fields=["name", "full_name", "creation"], limit_page_length=0)} if hits else {}
	for h in hits:
		c = contacts.get(h["contact"]) or {}
		h.update(full_name=c.get("full_name"), created=c.get("creation"))
	hits.sort(key=lambda h: str(h["created"] or ""), reverse=True)
	columns = [
		{"label": _("Contact"), "fieldname": "contact", "fieldtype": "Link", "options": CONTACT, "width": 200},
		{"label": _("Full Name"), "fieldname": "full_name", "fieldtype": "Data", "width": 200},
		{"label": _("Email"), "fieldname": "email", "fieldtype": "Data", "width": 260},
		{"label": _("Pattern"), "fieldname": "pattern", "fieldtype": "Data", "width": 150},
		{"label": _("Primary"), "fieldname": "is_primary", "fieldtype": "Check", "width": 80},
		{"label": _("Created"), "fieldname": "created", "fieldtype": "Datetime", "width": 160},
	]
	return columns, hits
