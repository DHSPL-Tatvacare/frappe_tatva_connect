# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CRMSmartView(Document):
	def validate(self):
		"""A write share edits the view, never who owns it or who it reaches: those stay the owner's and the sharer's."""
		from tatva_connect.smartview import permissions as sv_perms

		before = self.get_doc_before_save()
		if not before:
			return
		if self.has_value_changed("owner_user") and not sv_perms.owns(before):
			frappe.throw(_("Only the owner can hand this view to someone else."), frappe.PermissionError)
		if self.has_value_changed("is_standard") and not sv_perms.can_share(before):
			frappe.throw(_("Only someone who can share this view can offer it to the business line."), frappe.PermissionError)
