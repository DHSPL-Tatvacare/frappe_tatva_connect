# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One event in a person's day, never edited: a Check-in (their status is their newest Check-in row) or an Away period. Read and written only through `lead/checkin.py`."""
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, get_datetime, get_fullname, now_datetime

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
		self.update(describe(self))

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


def describe(row):
	"""The row in plain words, `{headline, back_at, idle_minutes}`, from its own columns; the one reader the save and the backfill share."""
	at = get_datetime(row.creation or now_datetime())
	if row.event != checkin.CHECK_IN:
		since = get_datetime(row.last_active_at) if row.last_active_at else None
		minutes = int((at - since).total_seconds() // 60) if since else 0
		span = f"{_clock(since, at)} → {_clock(at, since)}" if since else _clock(at)
		return {"headline": _("{0} · Idle {1} min").format(span, minutes), "back_at": at, "idle_minutes": minutes}
	by = {checkin.SELF: _("self"), checkin.SHIFT_END: _("shift end"), checkin.HELPDESK: _("Helpdesk"),
	      checkin.MANAGER: get_fullname(row.owner)}.get(row.source, row.source)
	headline = f"{_clock(at)} · " + _("Set {0}").format(_(row.status))
	return {"headline": f"{headline} · " + _("by {0}").format(by) if by else headline, "back_at": None, "idle_minutes": 0}


def _clock(moment, other=None):
	"""`HH:MM`, with the date when the gap crosses a day."""
	return moment.strftime("%d %b %H:%M" if other and other.date() != moment.date() else "%H:%M")


def on_doctype_update():
	# A status is a person's newest Check-in row, so every read seeks (user, event) and walks creation backwards.
	frappe.db.add_index("Tatva Activity Log", ["user", "event", "creation"])
