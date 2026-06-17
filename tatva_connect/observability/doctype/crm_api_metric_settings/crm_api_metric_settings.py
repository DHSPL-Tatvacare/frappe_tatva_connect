# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMAPIMetricSettings(Document):
	"""Single. Holds the rollup high-water mark, the safety lag, the 5-min retention
	window, and a small last-run breadcrumb. Operators may clear `last_rolled_until`
	to force a backfill."""

	pass
