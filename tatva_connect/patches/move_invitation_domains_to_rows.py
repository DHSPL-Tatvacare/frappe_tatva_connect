# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Tatva Platform Settings' two one-per-line invitation domain boxes become the Invitation Domains grid: each saved domain
becomes an Allowed or Blocked row, then the old Singles values go (frappe never clears a removed field's Singles row).
Idempotent: with no old values it does nothing. No schema_setup twin: a fresh site never had the boxes."""
import frappe

from tatva_connect.access import invitations

# Blocked first: a domain saved in both boxes stays Blocked, as Blocked always won.
_OLD = {"blocked_invitation_domains": "Blocked", "invitation_domains": "Allowed"}


def execute():
	saved = frappe.db.get_singles_dict(invitations.SETTINGS)  # raw: still holds the removed fields' values
	if not any((saved.get(field) or "").strip() for field in _OLD):
		frappe.db.delete("Singles", {"doctype": invitations.SETTINGS, "field": ["in", list(_OLD)]})
		return
	settings = frappe.get_single(invitations.SETTINGS)
	listed = {row.domain for row in settings.get(invitations.FIELD)}
	for field, rule in _OLD.items():
		for line in (saved.get(field) or "").splitlines():
			if line.strip() and (domain := invitations.clean_domain(line)) not in listed:
				settings.append(invitations.FIELD, {"domain": domain, "rule": rule})
				listed.add(domain)
	settings.flags.ignore_validate = True  # only the domains move; the form's other rules belong to its operator's next save
	settings.save(ignore_permissions=True)
	frappe.db.delete("Singles", {"doctype": invitations.SETTINGS, "field": ["in", list(_OLD)]})
