# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from tatva_connect.api._base import assert_within_ceiling


class CRMPartnerAPISettings(Document):
	"""Single. The numeric knobs for the gated partner API — rate-limit buckets
	(window / per-token / global / bulk) and payload/fetch caps (bulk size, list page,
	download timeout). On/off lives ONLY in the automation row
	`Partner::RateLimit::enforcement`; this form carries no switch. Blank fields fall
	back to the DEFAULTS in `tatva_connect.api._base`; the `0`-rules live in `_cfg()`.

	This form is wired straight to the live API — `_cfg()` re-reads it on every request, so a save
	takes effect on the very next call. Every knob takes any value; the only one
	`_base.assert_within_ceiling` still refuses is `bulk_burst`, a correctness invariant."""

	def validate(self):
		assert_within_ceiling(self)
