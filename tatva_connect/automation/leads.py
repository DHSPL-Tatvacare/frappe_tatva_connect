"""CRM Lead automations."""
import frappe
from frappe import _


def dedup_guard(doc, method=None):
	"""Block a second CRM Lead with the same ``mobile_no`` — platform-wide dedup.

	``mobile_no`` is the permanent dedup key. This fires on EVERY insert path
	(REST POST, the CRM "Create Lead" modal, imports, the intake Web Form). The
	``upsert_lead_by_phone`` endpoint finds-or-updates an existing number first,
	so it only ever inserts a genuinely new number and never trips this guard.

	No mobile_no -> nothing to dedup (some leads are email/name only).
	"""
	if not doc.mobile_no:
		return

	existing = frappe.db.get_value(
		"CRM Lead",
		{"mobile_no": doc.mobile_no, "name": ["!=", doc.name or ""]},
		"name",
	)
	if existing:
		frappe.throw(
			_("A lead with mobile number {0} already exists ({1}). Update that lead instead.").format(
				doc.mobile_no, existing
			),
			title=_("Duplicate lead"),
		)
