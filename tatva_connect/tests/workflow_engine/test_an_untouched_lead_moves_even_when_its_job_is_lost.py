# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead whose rep has not worked it (no started task, no outbound call) moves to another rep at the time saved on its journey, even when Redis lost the job (DA54)."""

import datetime
import time
from unittest.mock import patch
from urllib.parse import unquote

import frappe
from frappe.tests import freeze_time
from frappe.utils.background_jobs import create_job_id, get_queue
from frappe.utils.data import convert_utc_to_system_timezone

from tatva_connect.lead import assignment, routing
from tatva_connect.notifications import batch, dispatch
from tatva_connect.tests.workflow_engine import fixtures as fx
from tatva_connect.workflow_engine import ENGINE_SWITCH, wakeups

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

	def test_the_job_moves_the_lead_at_the_second_rq_wakes_it(self):
		lead, run, rep, _task = self._deal(fx.at(fx.MONDAY, 11).replace(microsecond=553000))
		# The booking goes to RQ as a commit would send it; RQ wakes the job at the start of its booked second.
		frappe.db.after_commit.run()
		queue = get_queue(wakeups.WAKE_QUEUE)
		held = wakeups._bookings(queue, create_job_id(f"reassign:{lead.name}"))
		self.addCleanup(wakeups._forget, queue, held)
		(wakes,) = held.values()

		self._run_check(run, convert_utc_to_system_timezone(wakes).replace(tzinfo=None))
		self.assertNotEqual(fx.holder(lead), rep)

	def test_the_owner_the_pool_chose_moves_with_the_lead(self):
		was = frappe.db.get_value("CRM Tatva Automation", assignment.OWNER_FROM_RULE, "enabled")
		fx.set_switch(assignment.OWNER_FROM_RULE, 1)
		self.addCleanup(fx.set_switch, assignment.OWNER_FROM_RULE, was)
		lead, run, rep, _task = self._deal(fx.at(fx.MONDAY, 14))
		self.assertEqual(frappe.db.get_value("CRM Lead", lead.name, "lead_owner"), rep)

		self._run_check(run, fx.at(fx.MONDAY, 14, 31))
		self.assertNotEqual(fx.holder(lead), rep)
		self.assertEqual(frappe.db.get_value("CRM Lead", lead.name, "lead_owner"), fx.holder(lead))

	def test_a_started_task_ends_the_checks(self):
		lead, run, rep, task = self._deal(fx.at(fx.MONDAY, 12))
		task.status = "In Progress"
		task.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, the rep starting their task

		self._run_check(run, fx.at(fx.MONDAY, 12, 31))
		self.assertEqual(fx.holder(lead), rep)
		self.assertIsNone(self._check_at(run))

	def _call(self, lead, caller):
		"""An outbound call on the lead by `caller`, as the telephony bridge logs a click-to-call."""
		return frappe.get_doc({
			"doctype": "CRM Call Log", "id": f"zz-call-{frappe.generate_hash(length=8)}", "type": "Outgoing", "status": "No Answer",
			"from": "+910000000001", "to": "+910000000002", "caller": caller, "reference_doctype": "CRM Lead", "reference_docname": lead.name,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, the call the rep's dialer logs

	def test_an_outbound_call_by_the_holder_ends_the_checks(self):
		lead, run, rep, _task = self._deal(fx.at(fx.MONDAY, 15))
		with freeze_time(fx.at(fx.MONDAY, 15, 10)):
			self._call(lead, rep)

		self._run_check(run, fx.at(fx.MONDAY, 15, 31))
		self.assertEqual(fx.holder(lead), rep)
		self.assertIsNone(self._check_at(run))

	def test_a_call_by_someone_else_still_moves_the_lead(self):
		lead, run, rep, _task = self._deal(fx.at(fx.MONDAY, 16))
		other = self.second if rep == self.first else self.first
		with freeze_time(fx.at(fx.MONDAY, 16, 10)):
			self._call(lead, other)

		self._run_check(run, fx.at(fx.MONDAY, 16, 31))
		self.assertNotEqual(fx.holder(lead), rep)

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

	def test_inside_a_bulk_action_a_rep_hears_once_per_wave_for_the_leads_moved_off_them(self):
		with patch("tatva_connect.tatva_connect.doctype.crm_list_action_job.crm_list_action_job.enqueue"):  # the record only, never a queued run
			job = frappe.get_doc({"doctype": "CRM List Action Job", "action": "Assign", "target_doctype": "CRM Lead"}).insert(ignore_permissions=True).name
		with batch.holding("CRM List Action Job", job):
			deals = [self._deal(fx.at(fx.MONDAY, 13)) for _ in range(4)]
		wave = frappe.as_json(["CRM List Action Job", job, "CRM Lead", dispatch.LEAD_MOVED], indent=None)
		keys = [batch._key("tally", wave), batch._key("last", wave), *(batch._key("names", wave, rep) for rep in (self.first, self.second))]
		self.addCleanup(batch._run, ("delete", *keys), ("srem", batch._key("open"), wave))
		self.enterContext(patch.object(dispatch, "armed", return_value=True))
		told = self.enterContext(patch("frappe.desk.doctype.notification_log.notification_log.enqueue_create_notification"))

		for _lead, run, _rep, _task in deals:
			self._run_check(run, fx.at(fx.MONDAY, 13, 31))
		self.assertFalse([c for c in told.call_args_list if c.args[1]["type"] == dispatch.LEAD_MOVED], "a moved lead was told on its own")
		batch._run(("setex", batch._key("last", wave), batch.KEEP_SECONDS, time.time() - batch._quiet_seconds() - 1))
		batch.flush()

		moved = {c.args[0]: c.args[1] for c in told.call_args_list if c.args[1]["type"] == dispatch.LEAD_MOVED}
		lost = {rep: sorted(lead.name for lead, _run, held, _task in deals if held == rep) for rep in (self.first, self.second)}
		self.assertEqual(set(moved), {rep for rep, leads in lost.items() if leads})
		for rep, notice in moved.items():
			count = len(lost[rep])
			self.assertEqual(notice["subject"], f"{count} {'lead' if count == 1 else 'leads'} moved to another rep, not worked in time")
			opens = unquote(notice["link"] or "") if count > 1 else notice["document_name"]
			self.assertTrue(all(lead in opens for lead in lost[rep]), "it opens exactly the leads moved off this rep")
