# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The rep's card names the reason the pool gives them no lead, read from the draw's own check, so the card and the draw always agree."""

from unittest.mock import call, patch

import frappe
from frappe.realtime import get_user_room
from frappe.tests import freeze_time, set_user

from tatva_connect.api import checkin as checkin_api
from tatva_connect.lead import assignment_rule as rules
from tatva_connect.lead import checkin, routing
from tatva_connect.tests.workflow_engine import fixtures as fx

TUESDAY = fx.MONDAY.replace(day=fx.MONDAY.day + 1)


class TestTheRepCardSaysWhyNoLeadComes(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.day = fx.shift("09:00:00", "17:00:00")

	def _rep(self, label, member=None, **pool):
		"""A rep alone in a check-in pool with the day shift; `member` sets their row, `pool` the pool."""
		user = fx.rep(label)
		rule = fx.pool([(user, 1, self.day, member or {})], require_checkin=1, **pool)
		return user, rule

	def _active(self, user, at):
		with freeze_time(at):
			checkin.record(user, checkin.ACTIVE, checkin.MANAGER)

	def _card_and_draw(self, user, rule, at):
		"""The card's lead flow and whether the draw gives this rep a lead, both at `at`; they must agree."""
		fx.fresh_request()
		lead = fx.leads(1)[0]
		with freeze_time(at):
			flow = checkin.lead_flow(user, at)
			drawn = frappe.get_doc("Assignment Rule", rule.name).get_credit_weighted_user(lead.as_dict()) == user
		self.assertEqual(flow["reason"] is None, drawn, f"card says {flow['reason']}, draw gave a lead: {drawn}")
		return flow

	def test_an_active_rep_on_shift_has_no_reason_and_gets_a_lead(self):
		user, rule = self._rep("card-eligible")
		self._active(user, fx.at(fx.MONDAY, 9, 5))
		flow = self._card_and_draw(user, rule, fx.at(fx.MONDAY, 16, 50))
		self.assertIsNone(flow["reason"])
		self.assertEqual(flow["ends_at"], fx.at(fx.MONDAY, 17))

	def test_after_the_shift_an_active_rep_reads_off_shift(self):
		user, rule = self._rep("card-after-hours")
		self._active(user, fx.at(fx.MONDAY, 18))
		flow = self._card_and_draw(user, rule, fx.at(fx.MONDAY, 18, 5))
		self.assertEqual(flow["reason"], rules.OFF_SHIFT)
		self.assertEqual(flow["next_at"], fx.at(TUESDAY, 9))

	def test_a_rep_not_checked_in_reads_not_checked_in(self):
		user, rule = self._rep("card-not-in")
		self.assertEqual(self._card_and_draw(user, rule, fx.at(fx.MONDAY, 10))["reason"], rules.NOT_CHECKED_IN)

	def test_a_rep_on_leave_reads_on_leave(self):
		user, rule = self._rep("card-leave")
		fx.leave(user, fx.MONDAY)
		self._active(user, fx.at(fx.MONDAY, 9, 5))
		self.assertEqual(self._card_and_draw(user, rule, fx.at(fx.MONDAY, 10))["reason"], rules.ON_LEAVE)

	def test_a_paused_member_reads_paused(self):
		user, rule = self._rep("card-paused", {"paused": 1})
		self._active(user, fx.at(fx.MONDAY, 9, 5))
		self.assertEqual(self._card_and_draw(user, rule, fx.at(fx.MONDAY, 10))["reason"], rules.PAUSED)

	def test_a_rep_at_the_shift_cap_reads_cap(self):
		user, rule = self._rep("card-cap", {"daily_cap": 1})
		self._active(user, fx.at(fx.MONDAY, 9, 5))
		with freeze_time(fx.at(fx.MONDAY, 10)):
			fx.distribute(rule, fx.leads(1)[0])
		self.assertEqual(self._card_and_draw(user, rule, fx.at(fx.MONDAY, 11))["reason"], rules.CAP)

	def test_a_rep_at_the_untouched_limit_reads_it_with_the_count(self):
		user, rule = self._rep("card-untouched", max_untouched=1)
		self._active(user, fx.at(fx.MONDAY, 9, 5))
		lead = fx.leads(1)[0]
		with freeze_time(fx.at(fx.MONDAY, 10)):
			fx.distribute(rule, lead)
			fx.task(lead.name, user)
		flow = self._card_and_draw(user, rule, fx.at(fx.MONDAY, 10, 20))
		self.assertEqual((flow["reason"], flow["untouched"], flow["limit"]), (rules.UNTOUCHED_LIMIT, 1, 1))

	def test_get_status_carries_the_reason_to_the_rep(self):
		self._checkin_on()
		user, _pool = self._rep("card-api")
		self._active(user, fx.at(fx.MONDAY, 18))
		fx.fresh_request()
		with freeze_time(fx.at(fx.MONDAY, 18, 5)), set_user(user):
			card = checkin_api.get_status()
		self.assertEqual((card["lead_flow"]["reason"], card["lead_flow"]["next_at"]), (rules.OFF_SHIFT, fx.at(TUESDAY, 9)))

	def test_a_rep_eligible_in_one_of_two_pools_has_no_reason_and_its_shift_end(self):
		user = fx.rep("card-two-pools")
		late = fx.shift("12:00:00", "20:00:00")
		fx.pool([(user, 1, late, {"paused": 1})], require_checkin=1)
		open_pool = fx.pool([(user, 1, self.day)], require_checkin=1)
		self._active(user, fx.at(fx.MONDAY, 12, 5))
		# The paused pool runs to 20:00, yet the countdown follows the pool that gives leads, which closes at 17:00.
		flow = self._card_and_draw(user, open_pool, fx.at(fx.MONDAY, 16))
		self.assertEqual((flow["reason"], flow["ends_at"]), (None, fx.at(fx.MONDAY, 17)))

	def _checkin_on(self):
		vertical = frappe.get_doc("CRM Vertical", fx.GRAIN["vertical"])
		vertical.checkin_enabled = 1
		vertical.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, the business line turning check-in on

	def test_the_shift_opening_tells_the_reps_open_tabs(self):
		user, _rule = self._rep("card-shift-open")
		fx.fresh_request()
		with freeze_time(fx.at(fx.MONDAY, 9)), patch("frappe.publish_realtime") as push:
			routing.open_shifts()
		self.assertIn(call(checkin.CARD_EVENT, room=get_user_room(user), after_commit=True), push.call_args_list)

	def test_yesterdays_active_gives_no_lead_and_asks_again_once_the_day_closes(self):
		self._checkin_on()
		was = frappe.db.get_value("CRM Tatva Automation", checkin.MIDNIGHT_CHECKOUT, "enabled")
		fx.set_switch(checkin.MIDNIGHT_CHECKOUT, 1)
		self.addCleanup(fx.set_switch, checkin.MIDNIGHT_CHECKOUT, was)
		user, rule = self._rep("card-overnight")
		self._active(user, fx.at(fx.MONDAY, 17, 5))
		fx.fresh_request()
		with freeze_time(fx.at(TUESDAY, 0, 1)):
			checkin.close_the_day()
		self.assertEqual(self._card_and_draw(user, rule, fx.at(TUESDAY, 9, 1))["reason"], rules.NOT_CHECKED_IN)
		fx.fresh_request()
		with freeze_time(fx.at(TUESDAY, 9, 1)), set_user(user):
			self.assertTrue(checkin_api.get_status()["prompt"])

	def test_after_the_shift_end_check_out_the_card_reads_off_shift(self):
		user, rule = self._rep("card-checked-out")
		self._active(user, fx.at(fx.MONDAY, 9, 5))
		with freeze_time(fx.at(fx.MONDAY, 17)):
			checkin.close_ended_shifts()
		self.assertEqual(checkin.status_of(user), checkin.UNAVAILABLE)
		self.assertEqual(self._card_and_draw(user, rule, fx.at(fx.MONDAY, 17, 2))["reason"], rules.OFF_SHIFT)
