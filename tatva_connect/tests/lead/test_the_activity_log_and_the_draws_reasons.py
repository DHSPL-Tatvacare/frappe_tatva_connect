# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Tatva Activity Log: an Away row is history and never a status, log clearing keeps each person's status row; and a draw names every member it skipped and why."""

import frappe
from frappe.utils import add_to_date, now_datetime

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
