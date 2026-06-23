# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMPartnerAPISettings(Document):
	"""Single. The numeric knobs for the gated partner API — rate-limit buckets
	(window / per-token / global) and payload/fetch caps (bulk size, list page,
	download timeout). On/off lives ONLY in the automation row
	`Partner::RateLimit::enforcement`; this form carries no switch. Blank fields fall
	back to the DEFAULTS in `tatva_connect.api._base`; the `0`-rules live in `_cfg()`."""

	pass
