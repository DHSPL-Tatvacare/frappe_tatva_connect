# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CRMAutomatableField(Document):
	"""The fail-closed write allowlist. validate() confirms the target doctype and field
	really exist, so a rule can never reference a phantom write target."""

	def validate(self):
		if not frappe.db.exists("DocType", self.target_doctype):
			frappe.throw(_("Target DocType {0} does not exist").format(self.target_doctype))
		meta = frappe.get_meta(self.target_doctype)
		if not meta.get_field(self.fieldname):
			frappe.throw(
				_("{0} has no field {1}").format(self.target_doctype, self.fieldname)
			)
