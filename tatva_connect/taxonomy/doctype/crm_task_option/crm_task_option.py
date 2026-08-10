# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

# The axes that make an option unique — the composite key, since the name is a hash.
_KEY_FIELDS = ("task_type", "fieldname", "option_value")


class CRMTaskOption(Document):
	def validate(self):
		# Whitespace is stripped so " Yes " and "Yes" cannot become two rows of the same value.
		for f in _KEY_FIELDS:
			v = self.get(f)
			if isinstance(v, str):
				self.set(f, v.strip() or None)
		if not self.display_label:
			self.display_label = self.option_value
		self._no_duplicate()

	def _no_duplicate(self):
		# Uniqueness is enforced here because a hash name cannot carry it the way `Program::Stage` does.
		clash = frappe.db.exists(self.doctype, {
			"task_type": self.task_type, "fieldname": self.fieldname,
			"option_value": self.option_value, "name": ("!=", self.name or ""),
		})
		if clash:
			frappe.throw(
				_("{0} already offers {1} for {2}.").format(self.task_type, self.option_value, self.fieldname),
				title=_("Duplicate option"),
			)
