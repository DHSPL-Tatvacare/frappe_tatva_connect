"""Phone canonicalisation for the WhatsApp channel.

One rule, one code path: numbers are stored canonical E.164 (`+<digits>`) and
reduced to comparable digits by `phone.match_digits` when two spellings must be matched. A
provider's subscriber id is bare digits and stock leads were stored as
`+91-XXXXXXXXXX`; without this they would never match.
"""
import frappe

from tatva_connect import phone as match


def to_e164(number: str, default_cc: str = "91") -> str:
	"""Canonical stored form: '+<digits>'. Strips hyphens/spaces; a bare 10-digit
	Indian mobile gets the country code prepended, so 9876543210, +91-9876543210 and
	919876543210 all become +919876543210. Empty -> returned unchanged."""
	digits = match.match_digits(number)
	if not digits:
		return number or ""
	if len(digits) == 10:
		digits = default_cc + digits
	return "+" + digits


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
