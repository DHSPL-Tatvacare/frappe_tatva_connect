# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One event in a person's day, never edited: a Check-in (their status is their newest Check-in row) or an Away period. Read and written only through `lead/checkin.py`."""
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, now_datetime

from tatva_connect import presence
from tatva_connect.lead import checkin, routing


class TatvaActivityLog(Document):
	def validate(self):
		if not self.is_new():
			frappe.throw(_("An activity row is never edited; add a new one instead."), title=_("Activity is a log"))
		self.event = self.event or checkin.CHECK_IN  # a row a person adds in Desk is a check-in
		# The shift-end job, the Helpdesk mirror and auto-away name their own source; a person is Self for their own row, Manager for anyone else's.
		source = self.flags.checkin_source
		if not source and self.user != frappe.session.user:
			frappe.only_for(checkin.managers(), message=True)
		self.source = source or (checkin.SELF if self.user == frappe.session.user else checkin.MANAGER)
		if self.event == checkin.CHECK_IN:
			self.last_active_at = presence.last_seen(self.user)  # an Away row brings its own: when the gap began

	def after_insert(self):
		if self.event != checkin.CHECK_IN:
			return  # an Away row is history only: it changes no status, mirrors nothing and wakes no pool
		checkin.mirror_to_helpdesk(self)
		if self.status == checkin.ACTIVE:
			routing.wake_pools_for(self.user)

	@staticmethod
	def clear_old_logs(days=90):
		"""Log Settings' contract: rows older than `days` go, except each person's newest Check-in row, which is their current status."""
		table = frappe.qb.DocType(checkin.DOCTYPE)
		old = table.creation < add_days(now_datetime(), -days)  # the clock `creation` is stamped by (the site's), not the database's
		if keep := [row.name for row in checkin.newest_check_ins()]:
			old = old & table.name.notin(keep)
		frappe.db.delete(table, filters=old)


def on_doctype_update():
	# A status is a person's newest Check-in row, so every read seeks (user, event) and walks creation backwards.
	frappe.db.add_index("Tatva Activity Log", ["user", "event", "creation"])
