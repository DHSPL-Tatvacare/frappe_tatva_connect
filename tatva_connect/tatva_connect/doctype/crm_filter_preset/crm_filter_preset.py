# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
import frappe
from frappe import _
from frappe.model.document import Document


class CRMFilterPreset(Document):
	def validate(self):
		self._one_label_per_surface_per_person()

	def _one_label_per_surface_per_person(self):
		"""Two presets of the same name on the same surface is a person overwriting themselves by accident.

		Enforced here rather than by a unique index because the surface is a Dynamic Link and `reference_name`
		is legitimately blank for a whole-list preset — an index over a nullable column would let duplicates
		through on exactly that case."""
		twin = frappe.db.exists("CRM Filter Preset", {
			"user": self.user, "reference_doctype": self.reference_doctype,
			"reference_name": self.reference_name or "", "label": self.label,
			"name": ("!=", self.name or ""),
		})
		if twin:
			frappe.throw(_("You already have a preset called {0} here.").format(self.label))
