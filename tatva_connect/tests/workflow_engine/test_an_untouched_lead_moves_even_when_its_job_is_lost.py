# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead whose rep has not started its task moves to another rep at the time saved on its journey, even when Redis lost the job (DA54)."""

import datetime

import frappe
from frappe.tests import freeze_time

from tatva_connect.lead import routing
from tatva_connect.tests.workflow_engine import fixtures as fx
from tatva_connect.workflow_engine import ENGINE_SWITCH

AFTER = '{"minutes": 30}'
TUESDAY = fx.MONDAY + datetime.timedelta(days=1)
WEDNESDAY = fx.MONDAY + datetime.timedelta(days=2)
THURSDAY = fx.MONDAY + datetime.timedelta(days=3)


class TestAnUntouchedLeadMovesEvenWhenItsJobIsLost(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		day = fx.shift("09:00:00", "17:00:00")
		cls.first, cls.second = fx.rep("reassign-first"), fx.rep("reassign-second")
		cls.pool = fx.pool([(cls.first, 1, day), (cls.second, 1, day)])
		fx.leave(cls.second, TUESDAY)

	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.set_user, frappe.session.user)

	def _deal(self, at):
		"""Distribute a new lead at `at` with a 30-minute reassign and raise its call task; the test never commits, so no job reaches Redis."""
		lead = fx.leads(1)[0]
		run = fx.journey(lead)
		with freeze_time(at):
			rep = fx.distribute(self.pool, lead, reassign_after=AFTER, journey=run)[1]
		return lead, run, rep, fx.task(lead.name, rep)

	def _run_check(self, run, at):
		"""The check the 15-minute sweep runs for `run` at `at`, with the engine on; `reassign_due` commits around it, so the test calls it directly."""
		fx.set_switch(ENGINE_SWITCH, 1)
		try:
			with freeze_time(at):
				routing.reassign_if_untouched(run)
		finally:
			fx.set_switch(ENGINE_SWITCH, 0)

	def _check_at(self, run):
		return frappe.db.get_value("CRM Workflow Journey", run, "reassign_at")

	def test_the_check_on_the_journey_moves_a_lead_whose_job_was_lost(self):
		lead, run, rep, _task = self._deal(fx.at(fx.MONDAY, 10))
		self.assertEqual(self._check_at(run), fx.at(fx.MONDAY, 10, 30))

		self._run_check(run, fx.at(fx.MONDAY, 10, 31))
		self.assertEqual(fx.holder(lead), self.second if rep == self.first else self.first)
		self.assertEqual(self._check_at(run), fx.at(fx.MONDAY, 11, 1))

	def test_a_started_task_ends_the_checks(self):
		lead, run, rep, task = self._deal(fx.at(fx.MONDAY, 12))
		task.status = "In Progress"
		task.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, the rep starting their task

		self._run_check(run, fx.at(fx.MONDAY, 12, 31))
		self.assertEqual(fx.holder(lead), rep)
		self.assertIsNone(self._check_at(run))

	def test_with_nobody_else_free_the_check_runs_again_one_delay_later(self):
		lead, run, rep, _task = self._deal(fx.at(TUESDAY, 10))
		self.assertEqual(rep, self.first)

		self._run_check(run, fx.at(TUESDAY, 10, 31))
		self.assertEqual(fx.holder(lead), self.first)
		self.assertEqual(self._check_at(run), fx.at(TUESDAY, 11, 1))

	def test_a_closed_pool_moves_the_check_to_its_next_opening(self):
		lead, run, rep, _task = self._deal(fx.at(WEDNESDAY, 16, 45))

		self._run_check(run, fx.at(WEDNESDAY, 17, 16))
		self.assertEqual(fx.holder(lead), rep)
		self.assertEqual(self._check_at(run), fx.at(THURSDAY, 9))
