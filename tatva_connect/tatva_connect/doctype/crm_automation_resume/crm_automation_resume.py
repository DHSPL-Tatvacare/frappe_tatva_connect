# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMAutomationResume(Document):
	"""The Wait park queue — one row per parked rule-fire segment: the remainder of a `Wait`
	action's run, bound to an immutable rule VERSION plus the `cursor` into its frozen action list and
	the segment's frozen context, waiting for `automation.resume.sweep_resume()` to resume it at/after
	`resume_at`. Pure data — inserted by `resume.park()`, resolved and flipped Done/Failed/Cancelled by
	the sweep. No controller logic; never seeded (A.17 — a runtime queue, not authored content)."""

	pass
