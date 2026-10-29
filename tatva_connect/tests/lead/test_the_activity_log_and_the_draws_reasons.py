# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Tatva Activity Log: an Away row is history and never a status, log clearing keeps each person's status row; a draw names every member it skipped and why; and a live call is working."""

import time

import frappe
from frappe.utils import add_to_date, now_datetime

from tatva_connect import presence
from tatva_connect.automation import actions, rules
from tatva_connect.automation import context as ctx_build
from tatva_connect.lead import assignment, checkin
from tatva_connect.tatva_connect.doctype.tatva_activity_log.tatva_activity_log import TatvaActivityLog
from tatva_connect.tests.workflow_engine import fixtures as fx


def _rows(user):
	return frappe.get_all(checkin.DOCTYPE, {"user": user}, ["event", "status", "source"], order_by="creation asc")


class TestTheActivityLogAndTheDrawsReasons(fx.PoolTestCase):
	def test_an_away_row_is_history_and_never_a_status(self):
		user = fx.rep("log-away")
		checkin.record(user, checkin.ACTIVE, checkin.MANAGER)
		away = checkin.record_away(user, add_to_date(now_datetime(), minutes=-10))
		self.assertEqual((away.event, away.status, away.source), (checkin.AWAY, checkin.AWAY, checkin.AUTO))
		self.assertTrue(away.last_active_at, "an Away row keeps when the gap began")
		# Every status reader still reads the Check-in row the Away row came after.
		self.assertEqual(checkin.status_of(user), checkin.ACTIVE)
		self.assertEqual(checkin.statuses({user}), {user: checkin.ACTIVE})
		self.assertEqual(checkin.status_at(user, now_datetime()), checkin.ACTIVE)
		self.assertEqual([r.event for r in _rows(user)], [checkin.CHECK_IN, checkin.AWAY])

	def test_one_return_writes_one_away_row(self):
		user = fx.rep("log-once")
		checkin.record(user, checkin.ACTIVE, checkin.MANAGER)
		away_from = add_to_date(now_datetime(), minutes=-10)
		self.assertTrue(checkin.record_away(user, away_from))
		self.assertIsNone(checkin.record_away(user, away_from), "a second report of the same return writes nothing")
		self.assertEqual([r.event for r in _rows(user)], [checkin.CHECK_IN, checkin.AWAY])

	def test_an_away_row_is_written_only_for_someone_checked_in(self):
		user = fx.rep("log-out")
		self.assertIsNone(checkin.record_away(user, add_to_date(now_datetime(), minutes=-10)), "never checked in")
		checkin.record(user, checkin.UNAVAILABLE, checkin.MANAGER)
		self.assertIsNone(checkin.record_away(user, add_to_date(now_datetime(), minutes=-10)), "checked out")
		self.assertEqual([r.event for r in _rows(user)], [checkin.CHECK_IN])

	def test_log_clearing_keeps_each_persons_status_row(self):
		user = fx.rep("log-clear")
		checkin.record(user, checkin.ACTIVE, checkin.MANAGER)
		checkin.record_away(user, add_to_date(now_datetime(), minutes=-10))
		checkin.record(user, checkin.AWAY, checkin.MANAGER)
		TatvaActivityLog.clear_old_logs(days=0)  # every row is older than now: only status rows may survive
		self.assertEqual([(r.event, r.status) for r in _rows(user)], [(checkin.CHECK_IN, checkin.AWAY)])
		self.assertEqual(checkin.status_of(user), checkin.AWAY)

	def test_a_draw_names_every_member_it_skipped_and_why(self):
		paused, open_rep = fx.rep("draw-paused"), fx.rep("draw-open")
		rule = fx.pool([(paused, 1, None, {"paused": 1}), (open_rep, 1, None)])
		lead = fx.leads(1)[0]
		state = ctx_build.context_for(lead, {})
		params = frappe._dict(assignment_rule=rule.name, action_type="Distribute")
		# The Distribute node's own handler, as the engine runs it; what it returns is the step row's text.
		text = actions.handler_of("Distribute")(params, lead.name, state.writing_as("d"), rules.lead_axes(lead.name), lead)
		self.assertEqual(text, f"assigned to {open_rep} · skipped: {paused}=paused")

	def test_the_skipped_text_is_capped(self):
		skipped = {f"rep-{i}@example.invalid": "off_shift" for i in range(200)}
		text = assignment.skipped_note(skipped)
		self.assertLessEqual(len(text), assignment.SKIPPED_CHARS + 40)
		self.assertRegex(text, r"\+\d+ more$")
		self.assertEqual(assignment.skipped_note({}), "")

	def test_every_working_source_is_hooked(self):
		"""A working source without its doc_events lines would never fire: the declaration is locked to the hooks."""
		events = frappe.get_hooks("doc_events")
		for doctype in presence.working():
			for event in ("after_insert", "on_update"):
				self.assertIn("tatva_connect.presence.on_working_doc", events.get(doctype, {}).get(event, []), f"{doctype}.{event}")

	def test_a_live_call_is_working_and_its_end_is_the_last_touch(self):
		rep = fx.rep("call-rep")
		call = frappe.get_doc({"doctype": "CRM Call Log", "id": f"probe-{frappe.generate_hash(length=10)}", "type": "Outgoing",
			"status": "In Progress", "from": "+910000000000", "to": "+910000000001", "caller": rep}).insert(ignore_permissions=True)
		self.assertLess(abs((now_datetime() - presence.last_seen(rep)).total_seconds()), 5, "a live call is working now")
		self.assertFalse(presence.is_active(rep), "on a call is not looking at the app, so push still reaches the phone")
		ended = add_to_date(now_datetime(), minutes=-3).replace(microsecond=0)
		call.status, call.end_time = "Completed", ended
		call.save(ignore_permissions=True)
		self.assertEqual(presence.last_seen(rep).replace(microsecond=0), ended, "the call's end is the last touch")

	def test_a_live_call_whose_end_never_arrived_stops_counting(self):
		rep = fx.rep("call-lost")
		frappe.cache.hset(presence._key(rep), "CRM Call Log:lost", {"state": presence.BUSY, "at": time.time() - presence.BUSY_SECONDS - 1})
		self.assertIsNone(presence.last_seen(rep))

	def test_a_transferred_call_frees_the_rep_it_left(self):
		first, second = fx.rep("call-first"), fx.rep("call-second")
		call = frappe.get_doc({"doctype": "CRM Call Log", "id": f"probe-{frappe.generate_hash(length=10)}", "type": "Incoming",
			"status": "In Progress", "from": "+910000000000", "to": "+910000000001", "receiver": first}).insert(ignore_permissions=True)
		call.receiver = second
		call.save(ignore_permissions=True)
		self.assertFalse(presence._live(presence._tabs(first), presence.BUSY, presence.BUSY_SECONDS), "the rep the call left is no longer on it")
		self.assertTrue(presence._live(presence._tabs(second), presence.BUSY, presence.BUSY_SECONDS))
