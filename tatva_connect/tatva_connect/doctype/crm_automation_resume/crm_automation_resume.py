# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMAutomationResume(Document):
	"""The Wait park queue (Task 9) — one row per parked rule-fire segment: a `Wait` action's
	remainder (rule + subject + the next action index + the segment's context) waiting for
	`automation.resume.sweep_resume()` to resume it at/after `resume_at`. Pure data — inserted by
	`resume.park()`, resolved and flipped Done/Failed by the sweep. No controller logic; never
	seeded (A.17 — a runtime queue, not authored content)."""

	pass
