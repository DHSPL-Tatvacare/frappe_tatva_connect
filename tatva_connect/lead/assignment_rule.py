# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Override of core `Assignment Rule`: gate CRM Lead assignment on grain.

A grain-tagged rule may only fire on a lead whose grain matches every SET axis; a blank axis is a wildcard.
Stock for non-CRM-Lead rules and for rules with no grain set. Vertical and group are mandatory on a CRM Lead
rule, `grain_program` is not — one Anaya rule must serve Sigrima, Ujvira, Tukavo and Nivolumab."""

import datetime
import functools

import frappe
from frappe import _
from frappe.automation.doctype.assignment_rule.assignment_rule import AssignmentRule
from frappe.cache_manager import clear_doctype_map, get_doctype_map, get_doctype_map_key
from frappe.utils import add_days, cint, get_datetime, get_time, get_weekday, getdate, now_datetime

from tatva_connect.access import request_cache

CREDIT_WEIGHTED = "Credit Weighted"
# Core's rule that takes its user from a field on the record, so it has no member grid to skip leave in.
BASED_ON_FIELD = "Based on Field"
LEAVE_DT, HOLIDAY_LIST_DT, SHIFT_DT = "Tatva User Leave", "CRM Holiday List", "Tatva Work Shift"
# Strategies whose members sit in the `weighted_users` grid, the one grid with a Work Shift column.
WEIGHTED = ("Weighted Distribution", CREDIT_WEIGHTED)
# Why a member cannot take a lead now, in the order the Credit Weighted draw checks; the rep's card shows the same reason.
OFF_SHIFT, ON_LEAVE, PAUSED, DISABLED, NOT_ENTITLED, CAP, NOT_CHECKED_IN, UNTOUCHED_LIMIT = (
	"off_shift", "on_leave", "paused", "disabled", "not_entitled", "cap", "not_checked_in", "untouched_limit")
# How far ahead `open_window` looks for the next opening before calling a pool unopenable.
HORIZON_DAYS = 60


try:
	# Helpdesk's own availability filter (Active, then Away, then everyone) stays in the chain when helpdesk is installed; core's class otherwise.
	from helpdesk.overrides.assignment_rule import HelpdeskAssignmentRule as _BaseRule
except ImportError:
	_BaseRule = AssignmentRule


class TatvaAssignmentRule(_BaseRule):
	def validate(self):
		super().validate()
		self._validate_work_shifts()

	def apply_assign(self, doc):
		# `apply_assign` is core's only assigning step on a save; a workflow pool is drawn from by its Distribute node instead.
		if self.get("assigned_by_workflow"):
			return False
		if self.document_type == "CRM Lead" and not self._lead_grain_matches(doc):
			return False
		return super().apply_assign(doc)

	def do_assignment(self, doc):
		# Core's step hardcodes its alert (assignment_rule.py `do_assignment`, `notify=True`); under an import's ticks the same pick goes through the system assignment.
		from tatva_connect.lead import assignment
		from tatva_connect.notifications import quiet

		if not quiet.bell_muted():
			return super().do_assignment(doc)
		doctype, name = doc.get("doctype"), doc.get("name")
		assignment.clear(doctype, name)
		user = self.get_user(doc)
		if not user or not frappe.db.exists("User", user):
			return False
		assignment.assign_for_system(doctype, name, user, note=frappe.render_template(self.description, doc), assignment_rule=self.name,
		                             date=doc.get(self.due_date_based_on) if self.due_date_based_on else None)
		self.db_set("last_user", user)  # as core does, for the next round-robin pick
		return True

	def get_user(self, doc):
		if self.rule == CREDIT_WEIGHTED:
			return self.get_credit_weighted_user(doc)
		if self.rule == BASED_ON_FIELD:
			return self._draw(doc)
		pool = "weighted_users" if self.rule in WEIGHTED else "users"
		members, at = self.get(pool), now_datetime()
		# One read of every member's leave, today and yesterday (a shift that crossed midnight began yesterday), as the Credit Weighted draw reads it.
		away = leaves([m.user for m in members], add_days(at.date(), -1), at.date(), self._axes())
		# The one shift test: an open day (Assignment Days, holiday lists), inside the member's shift if they carry one, and off leave.
		present = [m for m in members if self.on_shift_now(m.get("work_shift"), at, m.user, away) is not None]
		if not present:
			return None  # nobody is working now, so the record waits unassigned rather than reach someone away
		# Every rule draws from the members working now; the grid is narrowed for the draw and restored, as helpdesk's availability filter does.
		setattr(self, pool, present)
		try:
			return self._draw(doc)
		finally:
			setattr(self, pool, members)

	def _draw(self, doc):
		# Helpdesk's availability filter is for tickets; every other rule skips it straight to core's draw.
		return super().get_user(doc) if self.document_type == "HD Ticket" else AssignmentRule.get_user(self, doc)

	def get_credit_weighted_user(self, doc, exclude=None):
		"""Smooth weighted round robin over members who can take this lead now, eligibility read before the lock on the rule row; `exclude` sits out."""
		from tatva_connect.taxonomy import grain

		members = self._members()  # the rule's own rows, in grid order, from the cached rule frappe clears on its save
		axes = tuple(doc.get(column) or "" for column in grain.columns(self.document_type))
		at = now_datetime()
		# One read of every member's leave, today and yesterday (a shift that crossed midnight began yesterday), not one per member.
		away = leaves([m.user for m in members], add_days(at.date(), -1), at.date(), self._axes())
		eligible = [m for m in members if m.user != exclude and self._can_take_a_lead(m, axes, at, away)]
		if not eligible:
			return None

		stored = frappe.parse_json(
			frappe.db.get_value("Assignment Rule", self.name, "credits", for_update=True) or "{}"
		)
		# A removed member's credit is dropped, so a re-added user starts level.
		credits = {m.user: cint(stored.get(m.user)) for m in members}
		for member in eligible:
			credits[member.user] += member.weight or 1
		winner = max(eligible, key=lambda m: credits[m.user])
		credits[winner.user] -= sum(m.weight or 1 for m in eligible)
		frappe.db.set_value(
			"Assignment Rule", self.name, "credits", frappe.as_json(credits), update_modified=False
		)
		return winner.user

	def _can_take_a_lead(self, member, axes, at, away=None):
		return self.why_not(member, axes, at, away) is None

	def why_not(self, member, axes, at, away=None):
		"""The first reason `member` cannot take a lead at `at`, in the draw's own order with the costliest read last; None when they can."""
		from tatva_connect.access import entitlement
		from tatva_connect.lead import checkin, routing

		# On shift, off leave on the shift's start date, and under the cap counted from that start.
		started = self.on_shift_now(member.work_shift, at, member.user, away)
		if started is None:
			return OFF_SHIFT if self._window_start(member.work_shift, get_datetime(at)) is None else ON_LEAVE
		if member.paused:
			return PAUSED
		if not frappe.get_cached_value("User", member.user, "enabled"):
			return DISABLED
		if any(axes) and not entitlement.grain_entitled(axes, user=member.user):
			return NOT_ENTITLED
		# Cancelled ToDos count toward the cap: a lead handed on later in the shift was still received.
		if member.daily_cap and frappe.db.count(
			"ToDo", {"allocated_to": member.user, "assignment_rule": self.name, "creation": [">=", started]}
		) >= member.daily_cap:
			return CAP
		# A pool that requires check-in takes only members whose latest check-in is Active.
		if self.get("require_checkin") and not checkin.is_active(member.user):
			return NOT_CHECKED_IN
		# A member at the pool's limit of untouched leads, where the pool sets one, waits for room (DA57).
		if not routing.has_room(self, member.user):
			return UNTOUCHED_LIMIT
		return None

	def on_shift_now(self, work_shift, at, user=None, away=None):
		"""When the `work_shift` window holding `at` began, or None (also None while `user` is on leave); the one shift test. `away` is `leaves()` already read for the pool."""
		started = self._window_start(work_shift, get_datetime(at))
		if started is None or not user:
			return started
		on_leave_then = _spans(away.get(user), started.date()) if away is not None else self._on_leave(user, started.date())
		return None if on_leave_then else started

	def open_window(self, at=None):
		"""`(is_open, opens_at)`: is any member on shift at `at`, else the earliest instant one will be (None = never within the horizon).

		The ONE reader of work shifts, holiday lists and Assignment Days. A pool with none of them is always open."""
		at = get_datetime(at or now_datetime())
		shifts = {row.get("work_shift") for row in self._members()} or {None}
		if any(self._window_start(shift, at) for shift in shifts):
			return True, None
		opens = [start for shift in shifts if (start := self._next_start(shift, at))]
		return False, min(opens) if opens else None

	def keeps_hours(self):
		"""Does a member carry a work shift or a holiday list cover this pool? Without either the pool behaves exactly as before shifts existed."""
		return any(row.get("work_shift") for row in self._members()) or bool(self._holidays())

	def _members(self):
		return self.get("weighted_users" if self.rule in WEIGHTED else "users") or []

	def shift_bounds(self, shift, at, user=None):
		"""`(ends_at, next_start)`: when the window holding `at` closes (None off shift or on leave), and when the next one opens after it; a member with no shift is open all day, so neither."""
		if not shift:
			return None, None
		window = self._window(shift, at) if self.on_shift_now(shift, at, user) is not None else None
		ends_at = window[1] if window else None
		return ends_at, self._next_start(shift, ends_at or at)

	def _window_start(self, shift, at):
		"""When the window of `shift` holding `at` began, or None; a blank shift is the whole open day."""
		window = self._window(shift, at)
		return window[0] if window else None

	def _window(self, shift, at):
		"""`(start, end)` of the window of `shift` holding `at`, or None; a blank shift is the whole open day."""
		if not shift:
			return (get_datetime(at.date()), get_datetime(add_days(at.date(), 1))) if self._day_open(at.date()) else None
		# A window belongs to the day it starts, so one that crossed midnight began yesterday.
		for day in (at.date(), add_days(at.date(), -1)):
			for start, end in self._windows(shift, day):
				if start <= at < end:
					return start, end
		return None

	def _next_start(self, shift, at):
		"""The first window of `shift` starting after `at` on an open day, or None within the horizon."""
		for offset in range(HORIZON_DAYS):
			day = add_days(at.date(), offset)
			if not shift:
				if offset and self._day_open(day):
					return get_datetime(day)
				continue
			starts = [start for start, _end in self._windows(shift, day) if start > at]
			if starts:
				return min(starts)
		return None

	def _windows(self, shift, day):
		"""`(start, end)` of each window `shift` opens on `day`; none on a closed day. An end at or before the start runs into the next day."""
		if not self._day_open(day):
			return []
		found = []
		for row in _shift_hours(shift):
			if row.workday != get_weekday(day):
				continue
			start = datetime.datetime.combine(day, get_time(row.start_time))
			end = datetime.datetime.combine(day, get_time(row.end_time))
			found.append((start, end if end > start else add_days(end, 1)))
		return found

	def _day_open(self, day):
		"""A window may start on `day`: it is one of the pool's Assignment Days and no covering holiday list names it."""
		days = self.get_assignment_days()
		return (not days or get_weekday(day) in days) and getdate(day) not in self._holidays()

	def _holidays(self):
		"""Every date named by a holiday list whose grain covers this pool's; several lists add up."""
		axes = self._axes()
		return request_cache("tatva_connect:pool_holidays", axes, lambda: _holiday_dates(axes))

	def _on_leave(self, user, day):
		"""A leave row for `user` spans `day` and its grain covers this pool's."""
		return on_leave(user, day, self._axes())

	def _axes(self):
		from tatva_connect.taxonomy import grain

		return tuple(self.get(column) if column else None for column in grain.columns(self.doctype))

	def _validate_work_shifts(self):
		"""A member's shift must cover this pool's grain, the same rule a holiday list or a leave row is matched by."""
		for row in self.get("weighted_users") or []:
			if row.get("work_shift") and not _covering(SHIFT_DT, {"name": row.work_shift}, self._axes()):
				frappe.throw(
					_("Row {0}: shift {1} belongs to another grain than this pool.").format(row.idx, row.work_shift),
					title=_("Shift outside the pool's grain"),
				)

	def _lead_grain_matches(self, doc):
		for rule_field, lead_field in (
			("grain_vertical", "custom_vertical"),
			("grain_group", "custom_group"),
			("grain_program", "custom_current_program"),
		):
			rule_value = self.get(rule_field)
			if rule_value and doc.get(lead_field) != rule_value:
				return False
		return True


def on_leave(user, day, axes=None):
	"""A leave row for `user` spans `day`; with a pool's `axes` its grain must cover them, with none any leave counts."""
	return _spans(leaves([user], day, day, axes).get(user), day)


def leaves(users, first_day, last_day, axes=None):
	"""`{user: [(from_date, to_date)]}` for each leave row touching the days given; with a pool's `axes` its grain must cover them. THE leave read."""
	from tatva_connect.taxonomy import grain

	wanted, first_day, last_day, found = set(users), getdate(first_day), getdate(last_day), {}
	for spell in _running_leave(first_day):
		if spell.user in wanted and getdate(spell.from_date) <= last_day and (axes is None or grain.row_covers(LEAVE_DT, spell, axes)):
			found.setdefault(spell.user, []).append((spell.from_date, spell.to_date))
	return found


def clear_pool_map(doc, method=None):
	"""Drop the doctype map a pool reads this master through, now and again at commit or rollback, as frappe's `clear_document_cache` does."""
	clear = functools.partial(clear_doctype_map, doc.doctype)
	clear()
	frappe.db.after_commit.add(clear)  # a draw between this save and its commit rebuilds the map from the old rows
	frappe.db.after_rollback.add(clear)


def _running_leave(day):
	"""Every leave row still running on `day`, with the columns a draw reads: one read a day under frappe's doctype-map key, so `clear_pool_map` drops it."""
	from tatva_connect.taxonomy import grain

	fields = ["name", "user", "from_date", "to_date", *(col for col in grain.columns(LEAVE_DT) if col)]
	return frappe.client_cache.get_value(get_doctype_map_key(LEAVE_DT, f"running_from::{day}"),
	                                     generator=lambda: frappe.get_all(LEAVE_DT, filters={"to_date": [">=", day]}, fields=fields))


def _cached_rows(doctype, name, filters=None):
	"""Each row in a pool's doctype map, from the document cache; a row deleted since the map was built drops out."""
	for row in get_doctype_map(doctype, name, filters=filters):
		try:
			yield frappe.get_cached_doc(doctype, row.name)
		except frappe.DoesNotExistError:
			frappe.clear_last_message()  # an older snapshot rebuilt the map after the delete; the row is gone, so it covers nothing


def _spans(spells, day):
	"""Does one of these leave spells cover `day`; an unset start reads as open, as frappe's filter reads it."""
	day = getdate(day)
	return any((not start or getdate(start) <= day) and end and getdate(end) >= day for start, end in spells or ())


def _covering(doctype, filters, axes):
	"""Names of the `doctype` rows matching `filters` whose grain covers `axes`."""
	from tatva_connect.taxonomy import grain

	return [row.name for row in grain.rows_covering(doctype, axes, filters)]


def _holiday_dates(axes):
	"""Every date named by a holiday list whose grain covers `axes`: the lists from frappe's doctype map, each list from the document cache."""
	from tatva_connect.taxonomy import grain

	dates = set()
	for holiday_list in _cached_rows(HOLIDAY_LIST_DT, "every"):
		if grain.row_covers(HOLIDAY_LIST_DT, holiday_list, axes):
			dates.update(getdate(day.date) for day in holiday_list.holidays)
	return dates


def _shift_hours(shift):
	"""The shift's working hours, from the document cache frappe clears on the shift's save."""
	return frappe.get_cached_doc(SHIFT_DT, shift).working_hours
