"""CRM Lead automations."""
import frappe
from frappe import _

from tatva_connect.wati.phone import to_e164

# Phone-type fields on CRM Lead we keep canonical (+E.164). mobile_no is the dedup +
# WhatsApp-inbound match key; the rest are normalized for consistency.
PHONE_FIELDS = ("mobile_no", "phone", "custom_alternate_number", "custom_caregiver_phone")


def normalize_lead_phones(doc, method=None):
	"""Canonicalise phone fields to +E.164 on every write (validate), so dedup and
	WhatsApp-inbound lookup are reliable no matter how a writer formatted the number.
	Runs BEFORE dedup_guard (hooks.py orders them)."""
	for f in PHONE_FIELDS:
		val = doc.get(f)
		if val:
			doc.set(f, to_e164(val))


def dedup_guard(doc, method=None):
	"""Block a duplicate CRM Lead on the same product line — per-line dedup.

	The dedup anchor is ``mobile_no + custom_vertical + custom_group``: one lead =
	one patient on one product-line+group. The SAME phone on a DIFFERENT line/group
	is a separate lead by design, so the guard only trips when all three match.

	Fires on EVERY insert path (REST POST, the CRM "Create Lead" modal, imports,
	the intake Web Form). The ``lead_create`` endpoint finds-or-updates an existing
	(phone, line, group) lead first, so a re-send never trips this guard.

	No mobile_no -> nothing to dedup (some leads are email/name only).
	"""
	if not doc.mobile_no:
		return

	mobile = to_e164(doc.mobile_no)  # compare on the canonical form (stored leads are canonical)
	existing = frappe.db.get_value(
		"CRM Lead",
		{
			"mobile_no": mobile,
			"custom_vertical": doc.custom_vertical,
			"custom_group": doc.custom_group,
			"name": ["!=", doc.name or ""],
		},
		"name",
	)
	if existing:
		frappe.throw(
			_(
				"A lead with mobile number {0} already exists on this product line ({1}). "
				"Update that lead instead."
			).format(mobile, existing),
			title=_("Duplicate lead"),
		)
