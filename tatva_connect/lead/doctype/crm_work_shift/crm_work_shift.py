# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""A named set of working hours for one grain, picked per member on a Distribute pool. Read only through `TatvaAssignmentRule.open_window`."""
import frappe
from frappe import _
from frappe.model.document import Document


class CRMWorkShift(Document):
	def validate(self):
		for row in self.working_hours:
			# Equal times would mean either nothing or a full day; neither is a shift anyone would type on purpose.
			if row.start_time == row.end_time:
				frappe.throw(
					_("Row {0}: the shift starts and ends at the same time. Use an end earlier than the start for a shift that crosses midnight.").format(row.idx),
					title=_("Invalid working hours"),
				)
