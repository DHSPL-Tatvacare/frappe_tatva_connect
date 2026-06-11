"""One-time canonicalisation of CRM Lead phone fields to +E.164.

Existing/migrated leads were stored like '+91-XXXXXXXXXX'; the WhatsApp inbound
lookup and dedup key on the canonical '+91XXXXXXXXXX' form. This sweeps existing
rows so they match. Idempotent (re-running is a no-op once canonical).
"""
import frappe

from tatva_connect.whatsapp import phone

FIELDS = ["mobile_no", "phone", "custom_alternate_number", "custom_caregiver_phone"]


def execute():
	for f in FIELDS:
		if frappe.db.has_column("CRM Lead", f):
			phone.sweep("CRM Lead", f)
