# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMPartnerAPIIdempotency(Document):
	"""Store for partner-API write idempotency: one row per (partner, Idempotency-Key). The name is
	sha1(partner:key), so a duplicate insert is the concurrency lock. Pure data — the _api wrapper
	claims/replays; a scheduled purge drops expired rows. Never operator-edited."""

	pass
