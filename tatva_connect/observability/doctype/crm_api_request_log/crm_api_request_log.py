# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMAPIRequestLog(Document):
	"""Raw request-level log. Pure data — written by observability.capture, read by
	observability.rollup. No controller logic (kept light for the insert hot path)."""

	pass
