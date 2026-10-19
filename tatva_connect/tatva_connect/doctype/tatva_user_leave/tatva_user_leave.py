# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""A user's days off: every assignment rule whose grain this row covers skips them on those days; read only through `TatvaAssignmentRule`."""
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

from tatva_connect.lead import assignment_rule
from tatva_connect.taxonomy import grain


class TatvaUserLeave(Document):
	def on_update(self):
		assignment_rule.clear_pool_map(self)

	def on_trash(self):
		assignment_rule.clear_pool_map(self)

	def validate(self):
		if getdate(self.to_date) < getdate(self.from_date):
			frappe.throw(_("The leave ends before it starts."), title=_("Invalid dates"))
		self._refuse_overlap()

	def _refuse_overlap(self):
		"""Two rows covering the same day for the same user and an overlapping grain say the same thing twice."""
		columns = grain.columns(self.doctype)
		mine = dict(zip(grain.AXES, (self.get(c) if c else None for c in columns), strict=True))
		for other in frappe.get_all(
			self.doctype,
			filters={"user": self.user, "name": ["!=", self.name or ""], "from_date": ["<=", self.to_date], "to_date": [">=", self.from_date]},
			fields=["name", *(c for c in columns if c)],
		):
			if grain.overlaps(mine, *(other.get(c) if c else None for c in columns)):
				frappe.throw(_("{0} already has leave on these days ({1}).").format(self.user, other.name), title=_("Overlapping leave"))
