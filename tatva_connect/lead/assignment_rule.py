# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Override of core `Assignment Rule`: gate CRM Lead assignment on grain.

A grain-tagged rule may only fire on a lead whose grain matches every SET axis; a blank axis is a wildcard.
Stock for non-CRM-Lead rules and for rules with no grain set. Vertical and group are mandatory on a CRM Lead
rule, `grain_program` is not — one Anaya rule must serve Sigrima, Ujvira, Tukavo and Nivolumab."""

import datetime

import frappe
from frappe import _
from frappe.automation.doctype.assignment_rule.assignment_rule import AssignmentRule
from frappe.utils import add_days, cint, get_datetime, get_time, get_weekday, getdate, now_datetime

from tatva_connect.access import request_cache

CREDIT_WEIGHTED = "Credit Weighted"
# Strategies whose members sit in the `weighted_users` grid, the one grid with a Work Shift column.
_WEIGHTED = ("Weighted Distribution", CREDIT_WEIGHTED)
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
		self._validate_untouched_limit()

	def apply_assign(self, doc):
		# `apply_assign` is core's only assigning step on a save; a workflow pool is drawn from by its Distribute node instead.
		if self.get("assigned_by_workflow"):
			return False
		if self.document_type == "CRM Lead" and not self._lead_grain_matches(doc):
			return False
		return super().apply_assign(doc)

	def get_user(self, doc):
		if self.rule == CREDIT_WEIGHTED:
			return self.get_credit_weighted_user(doc)
		# Helpdesk's availability filter is for tickets; every other rule skips it straight to core's draw.
		if self.document_type == "HD Ticket":
			return super().get_user(doc)
		return AssignmentRule.get_user(self, doc)

	def get_credit_weighted_user(self, doc, exclude=None):
		"""Smooth weighted round robin over the members who can take this lead now, locked on the rule row like core's `current_index`; `exclude` is a member left out of this draw.

		Eligibility is settled BEFORE the lock: entitlement and daily cap are a read per member, and holding the
		pool through them is what turned a draw into a queue. The lock covers the counter alone."""
		from tatva_connect.taxonomy import grain

		members = frappe.get_all(
			"Assignment Rule User",
			filters={"parenttype": "Assignment Rule", "parent": self.name, "parentfield": "weighted_users"},
			fields=["user", "weight", "daily_cap", "paused", "work_shift"],
			order_by="idx asc",
		)
		axes = tuple(doc.get(column) or "" for column in grain.columns(self.document_type))
		at = now_datetime()
		eligible = [m for m in members if m.user != exclude and self._can_take_a_lead(m, axes, at)]
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

	def _can_take_a_lead(self, member, axes, at):
		from tatva_connect.access import entitlement
		from tatva_connect.lead import checkin, routing

		# On shift, off leave on the shift's start date, and under the cap counted from that start.
		started = self.on_shift_now(member.work_shift, at, member.user)
		# Cancelled ToDos count toward the cap: a lead handed on later in the shift was still received.
		return (
			started is not None
			and not member.paused
			and frappe.get_cached_value("User", member.user, "enabled")
			and (not any(axes) or entitlement.grain_entitled(axes, user=member.user))
			and not (
				member.daily_cap
				and frappe.db.count(
					"ToDo",
					{"allocated_to": member.user, "assignment_rule": self.name, "creation": [">=", started]},
				) >= member.daily_cap
			)
			# A pool that requires check-in takes only members whose latest check-in is Active.
			and (not self.get("require_checkin") or checkin.is_active(member.user))
			# A member at the pool's limit of untouched leads waits for room (DA55); checked last, it is the costliest read.
			and routing.has_room(self, member.user)
		)

	def on_shift_now(self, work_shift, at, user=None):
		"""When the window of `work_shift` holding `at` began, or None; with `user`, also None while they are on leave that day.

		The ONE shift test: a draw and a check-in both ask it, so "on shift" means the same thing to each."""
		started = self._window_start(work_shift, get_datetime(at))
		if started is None or (user and self._on_leave(user, started.date())):
			return None
		return started

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
		return self.get("weighted_users" if self.rule in _WEIGHTED else "users") or []

	def _window_start(self, shift, at):
		"""When the window of `shift` holding `at` began, or None; a blank shift is the whole open day."""
		if not shift:
			return get_datetime(at.date()) if self._day_open(at.date()) else None
		# A window belongs to the day it starts, so one that crossed midnight began yesterday.
		for day in (at.date(), add_days(at.date(), -1)):
			for start, end in self._windows(shift, day):
				if start <= at < end:
					return start
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

	def _validate_untouched_limit(self):
		"""A Credit Weighted pool's limit on untouched leads per rep is a whole number of at least one; blank reads as the default."""
		if self.rule == CREDIT_WEIGHTED and self.get("max_untouched") is not None and cint(self.max_untouched) < 1:
			frappe.throw(_("Max untouched leads per rep must be 1 or more."), title=_("Invalid limit"))

	def _validate_work_shifts(self):
		"""A member's shift must cover this pool's grain, the same rule a holiday list or a leave row is matched by."""
		for row in self.get("weighted_users") or []:
			if row.get("work_shift") and not _covering("Tatva Work Shift", {"name": row.work_shift}, self._axes()):
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
	filters = {"user": user, "from_date": ["<=", day], "to_date": [">=", day]}
	if axes is None:
		return bool(frappe.db.exists("Tatva User Leave", filters))
	return bool(_covering("Tatva User Leave", filters, axes))


def _covering(doctype, filters, axes):
	"""Names of the `doctype` rows matching `filters` whose grain covers `axes`, the grain columns read off the schema."""
	from tatva_connect.taxonomy import grain

	columns = grain.columns(doctype)
	rows = frappe.get_all(doctype, filters=filters, fields=["name", *(c for c in columns if c)])
	return [
		row.name
		for row in rows
		if grain.covers(dict(zip(grain.AXES, (row.get(c) if c else None for c in columns), strict=True)), *axes)
	]


def _holiday_dates(axes):
	lists = _covering("CRM Holiday List", {}, axes)
	return {getdate(d) for d in frappe.get_all("CRM Holiday", filters={"parenttype": "CRM Holiday List", "parent": ["in", lists]}, pluck="date")} if lists else set()


def _shift_hours(shift):
	return request_cache("tatva_connect:shift_hours", shift, lambda: frappe.get_all(
		"CRM Service Day",
		filters={"parenttype": "Tatva Work Shift", "parent": shift, "parentfield": "working_hours"},
		fields=["workday", "start_time", "end_time"],
	))
