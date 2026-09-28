# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.intake import layers


class CRMIntakeFieldMap(Document):
	def validate(self):
		table = (self.target_table or "").strip()
		if not table or table == "note":
			return  # blank (layout field) and note (free-text) are not sections
		if any(table in layers.destinations(target) for target in layers.LAYERS):
			return  # a layer form's record; its parent checks it against that form's target
		if not frappe.db.exists("CRM Lead Section", table):
			frappe.throw(
				_("Target Table '{0}' is not a known CRM Lead Section (nor blank/note).").format(table),
				title=_("Unknown Target Table"),
			)
		# The catalogue + grain check lives on the PARENT (CRMIntakeForm._validate_targets): a child controller's validate() is not invoked by a parent save, so a gate here would never fire.
