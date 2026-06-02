"""Phone canonicalisation for WATI.

One rule, one code path: numbers are stored canonical E.164 (`+<digits>`) and
reduced to bare digits only at the WATI boundary (api.normalize_number). WATI's
`waId` is bare digits and stock leads were stored as `+91-XXXXXXXXXX`; without
this they would never match.
"""
import frappe

from tatva_connect.wati.api import normalize_number


def to_e164(number: str) -> str:
	"""Canonical stored form: '+<digits>' (strip hyphens/spaces, keep one '+')."""
	digits = normalize_number(number)
	return ("+" + digits) if digits else (number or "")


def sweep(doctype: str = "CRM Lead", field: str = "mobile_no") -> dict:
	"""One-time canonicalisation of existing rows to E.164. Direct DB writes
	(update_modified=False) so we don't fire controllers/notifications."""
	rows = frappe.get_all(doctype, filters={field: ["is", "set"]}, fields=["name", field])
	changed = 0
	for r in rows:
		old = r.get(field) or ""
		new = to_e164(old)
		if new != old:
			frappe.db.set_value(doctype, r.name, field, new, update_modified=False)
			changed += 1
	frappe.db.commit()
	return {"scanned": len(rows), "changed": changed}
