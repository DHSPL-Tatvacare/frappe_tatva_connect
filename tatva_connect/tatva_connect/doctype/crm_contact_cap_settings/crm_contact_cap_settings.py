# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The operator's ceiling on how often ONE NUMBER is contacted. A Single, and dormant at rest."""
import frappe
from frappe import _
from frappe.model.document import Document

# The band the product owner set. Below 1 the cap would silence the engine for everyone; above 20 it stops
# being a guard against pestering and becomes a number nobody chose. Refused rather than clamped: a value
# quietly rewritten is an operator who believes they set something they did not.
MIN_CONTACTS, MAX_CONTACTS = 1, 20

# What each unit is worth in days. Declared, because "a month" has to mean one thing to the counter and to
# the operator reading the page, and 30 is what "in a month" means to the person being messaged.
DAYS_PER_UNIT = {"Days": 1, "Weeks": 7, "Months": 30}


class CRMContactCapSettings(Document):
	def validate(self):
		if not MIN_CONTACTS <= (self.max_contacts or 0) <= MAX_CONTACTS:
			frappe.throw(
				_("Enter between {0} and {1} messages. {2} is outside what this cap can be set to.").format(
					MIN_CONTACTS, MAX_CONTACTS, self.max_contacts,
				),
				title=_("Outside the range"),
			)
		if (self.window_count or 0) < 1:
			frappe.throw(_("The period must be at least 1 {0}.").format(self.window_unit or _("day")),
			             title=_("Period too short"))

	def window_days(self) -> int:
		"""How far back the rolling count reaches, in days."""
		return (self.window_count or 0) * DAYS_PER_UNIT.get(self.window_unit or "Days", 1)
