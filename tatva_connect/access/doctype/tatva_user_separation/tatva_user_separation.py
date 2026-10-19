# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One user's separation, planned on its first save and run by `access.separation`; the record is the audit of what moved."""
import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.access import separation


class TatvaUserSeparation(Document):
	def onload(self):
		if not self.is_new():
			self.set_onload("review", separation.review(self))

	def validate(self):
		self._hold_while_running()
		if self.reassign_to == self.user:
			frappe.throw(_("Reassign To must be a different user."))
		other = frappe.db.exists(self.doctype, {"user": self.user, "name": ["!=", self.name], "status": ["in", ("Draft", "Running")]})
		if other:
			frappe.throw(_("{0} already has an open separation: {1}.").format(self.user, frappe.utils.get_link_to_form(self.doctype, other)))
		if self.is_new():
			separation.plan(self)
		if any(not row.ref_doctype for row in self.work):
			frappe.throw(_("A new work row needs its group: use Split on an existing row."))
		self._follow_reassign_to()
		separation.share_out(self)

	def _hold_while_running(self):
		"""A run reads and writes this record as it goes, so only its own worker saves it until the run ends."""
		before = self.get_doc_before_save()
		if before and before.status == "Running" and not self.flags.separation_worker:
			frappe.throw(_("This separation is running. Wait for it to finish before changing it."), title=_("Run in progress"))

	def _follow_reassign_to(self):
		"""A new Reassign To takes every work row not yet run that still named the old one."""
		before = self.get_doc_before_save()
		if not before or before.reassign_to == self.reassign_to:
			return
		for row in self.work:
			if row.status == "Pending" and row.assign_to in (None, "", before.reassign_to):
				row.assign_to = self.reassign_to
