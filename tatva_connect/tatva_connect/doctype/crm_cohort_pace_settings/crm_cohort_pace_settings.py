# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The operator's pace for a scheduled cohort: how many journeys per batch, and how long between batches.

A Single, mirroring `CRM Contact Cap Settings` — the two are the same kind of thing, an operator-facing
number the engine paces itself by, and they are read the same way and validated the same way.

WHY THIS IS A DOCTYPE AND NOT A CONSTANT. `thresholds` says knobs are constants unless someone would change
one AT RUNTIME. Here they would: the pace is set from what a real cohort does to the provider and to the
workers, which is not knowable until one has run. `thresholds` keeps the defaults a site starts from.
"""
import frappe
from frappe import _
from frappe.model.document import Document

# Refused rather than clamped, exactly as the contact cap refuses: a value quietly rewritten is an operator
# who believes they set something they did not. The bands are what a cohort can be paced at and still be a
# cohort — below them it never finishes, above them it stops being paced at all.
BANDS = {"chunk_size": (1, 500), "interval_seconds": (10, 3600)}


class CRMCohortPaceSettings(Document):
	def validate(self):
		for fieldname, (low, high) in BANDS.items():
			value = self.get(fieldname) or 0
			if not low <= value <= high:
				frappe.throw(
					_("{0} must be between {1} and {2}. {3} is outside what the pace can be set to.").format(
						self.meta.get_label(fieldname), low, high, value,
					),
					title=_("Outside the range"),
				)

	def pace(self) -> tuple[int, int]:
		"""`(patients per batch, seconds until the next one)` — the pair the drain asks for.

		The declared defaults are applied HERE and nowhere else, because a Single that has never been saved
		reads back empty and every caller would otherwise carry its own copy of the fallback.
		"""
		from tatva_connect.workflow_engine import thresholds

		return (
			self.chunk_size or thresholds.DRAIN_CHUNK,
			self.interval_seconds or thresholds.DRAIN_INTERVAL_SECONDS,
		)
