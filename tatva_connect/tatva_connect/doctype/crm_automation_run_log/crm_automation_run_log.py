# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMAutomationRunLog(Document):
	"""Audit/observability log of every automation rule fire. Pure data — written by the
	dispatcher in its own try/except. No controller logic."""

	pass
