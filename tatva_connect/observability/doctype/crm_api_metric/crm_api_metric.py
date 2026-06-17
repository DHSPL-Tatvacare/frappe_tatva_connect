# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMAPIMetric(Document):
	"""Immortal aggregate metric bucket. Rows are written/updated by observability.rollup
	via a raw INSERT ... ON DUPLICATE KEY UPDATE keyed on name = SHA1(grain), so this
	controller carries no logic. Every stored column is additive."""

	pass
