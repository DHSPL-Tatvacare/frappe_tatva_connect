# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One status change for one person, never edited; their status is their latest row. Read and written only through `lead/checkin.py`."""
import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.lead import checkin, routing
from tatva_connect.notifications import presence


class TatvaUserCheckin(Document):
	def validate(self):
		if not self.is_new():
			frappe.throw(_("A check-in is never edited; add a new one instead."), title=_("Check-in is a log"))
		# The shift-end job and the Helpdesk mirror name their own source; a person is Self for their own row, Manager for anyone else's.
		source = self.flags.checkin_source
		if not source and self.user != frappe.session.user:
			frappe.only_for(checkin.managers(), message=True)
		self.source = source or (checkin.SELF if self.user == frappe.session.user else checkin.MANAGER)
		self.last_active_at = presence.last_seen(self.user)

	def after_insert(self):
		checkin.mirror_to_helpdesk(self)
		if self.status == checkin.ACTIVE:
			routing.wake_pools_for(self.user)


def on_doctype_update():
	# A status is the latest row for a user, so every read seeks (user) and walks creation backwards.
	frappe.db.add_index("Tatva User Checkin", ["user", "creation"])
