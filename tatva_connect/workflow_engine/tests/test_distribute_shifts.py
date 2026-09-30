# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Distribute leaves by `closed` with the next opening when no member is on shift, driven through `interpreter._run_verb`."""

import datetime
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_weekday

from tatva_connect.automation import context as ctx_build
from tatva_connect.tests.authz.grains import GRAINS
from tatva_connect.workflow_engine import interpreter, refs
from tatva_connect.workflow_engine.tests import fixtures as fx

MONDAY = datetime.date(2030, 1, 7)
OTHER = next(g for g in GRAINS if g["vertical"] != fx.GRAIN["vertical"])
_CACHES = ("tatva_connect:pool_holidays", "tatva_connect:shift_hours")


def _at(day, hour):
	return datetime.datetime.combine(day, datetime.time(hour))


class TestDistributeShifts(FrappeTestCase):
	def setUp(self):
		fx.roll_back_pools(self)
		self._forget()
		self.addCleanup(self._forget)
		self.a, self.b = (fx.make_user(f"shift-probe-{i}@example.invalid") for i in "ab")
		for rep in (self.a, self.b):
			frappe.get_doc("User", rep).add_roles("Sales User")

	def _shift(self, start, end, grain=fx.GRAIN):
		days = [MONDAY + datetime.timedelta(days=i) for i in range(7)]
		return frappe.get_doc({
			"doctype": "CRM Work Shift", "shift_name": f"probe {frappe.generate_hash(length=6)}",
			"grain_vertical": grain.get("vertical"), "grain_group": grain.get("group"), "grain_program": grain.get("program"),
			"working_hours": [{"workday": get_weekday(d), "start_time": start, "end_time": end} for d in days],
		}).insert(ignore_permissions=True).name  # authz-ok: tier-c — test fixture, no user input

	def _pool(self, shift):
		return fx.make_pool([{"user": u, "weight": 1, "work_shift": shift} for u in (self.a, self.b)], assigned_by_workflow=1)

	def _fire(self, pool, now):
		node = frappe._dict({
			"node_id": "d", "node_type": "Distribute", "edges": [],
			"config_json": frappe.as_json({"assignment_rule": pool.name}),
		})
		lead = fx.make_lead()
		state = ctx_build.context_for(lead, {})
		with patch("tatva_connect.lead.assignment_rule.now_datetime", return_value=now):
			interpreter._run_verb(node, lead.name, lead, state, fx.AXES)
		view = state.writing_as("d")
		return view.get(refs.OUTPUT), view.get("d.opens_at")

	def test_off_shift_leaves_by_closed_until_the_shift_opens_then_assigns(self):
		pool = self._pool(self._shift("08:00:00", "20:00:00"))
		self.assertEqual(self._fire(pool, _at(MONDAY, 2)), ("closed", _at(MONDAY, 8)))
		self.assertEqual(self._fire(pool, _at(MONDAY, 9)), ("assigned", None))

	def test_a_shift_crossing_midnight_is_open_after_midnight_and_closed_after_it_ends(self):
		pool = self._pool(self._shift("22:00:00", "06:00:00"))
		self.assertEqual(self._fire(pool, _at(MONDAY, 2))[0], "assigned")
		self.assertEqual(self._fire(pool, _at(MONDAY, 7)), ("closed", _at(MONDAY, 22)))

	def test_a_holiday_list_closes_only_the_pools_its_grain_covers(self):
		pool = self._pool(self._shift("08:00:00", "20:00:00"))
		self._holiday_list(OTHER["vertical"])
		self.assertEqual(self._fire(pool, _at(MONDAY, 9))[0], "assigned")
		self._holiday_list(None)
		self._forget()
		self.assertEqual(self._fire(pool, _at(MONDAY, 9)), ("closed", _at(MONDAY + datetime.timedelta(days=1), 8)))

	def test_a_member_on_leave_gets_no_lead_and_the_other_member_does(self):
		pool = self._pool(self._shift("08:00:00", "20:00:00"))
		frappe.get_doc({"doctype": "CRM User Leave", "user": self.a, "from_date": MONDAY, "to_date": MONDAY}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture
		for _ in range(3):
			self.assertEqual(self._fire(pool, _at(MONDAY, 9))[0], "assigned")
		self.assertEqual(set(frappe.get_all("ToDo", {"assignment_rule": pool.name}, pluck="allocated_to")), {self.b})

	def _holiday_list(self, vertical):
		frappe.get_doc({
			"doctype": "CRM Holiday List", "holiday_list_name": f"probe {frappe.generate_hash(length=6)}",
			"from_date": MONDAY, "to_date": MONDAY + datetime.timedelta(days=30), "grain_vertical": vertical,
			"holidays": [{"date": MONDAY, "description": "probe"}],
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture

	@staticmethod
	def _forget():
		# Holidays and shift hours are cached per request; a test that adds one mid-way starts a new request.
		for bucket in _CACHES:
			if hasattr(frappe.local, bucket):
				delattr(frappe.local, bucket)
