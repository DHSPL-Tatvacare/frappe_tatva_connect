# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every assignment rule skips a person on leave and closes on a holiday, not only a Credit Weighted rule; a ticket rule reads only leave and holidays that apply everywhere.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.lead.test_every_rule_skips_leave
"""
import frappe
from frappe.cache_manager import clear_doctype_map
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from tatva_connect.tests.workflow_engine.fixtures import fresh_request, holiday_list
from tatva_connect.workflow_engine.tests.fixtures import GRAIN, make_user

ASHA, RAVI = "zz-leave-asha@example.com", "zz-leave-ravi@example.com"


class TestEveryRuleSkipsLeave(IntegrationTestCase):
	def setUp(self):
		for user in (ASHA, RAVI):
			make_user(user)
		fresh_request()

	def tearDown(self):
		frappe.db.rollback()
		for master in ("Tatva User Leave", "CRM Holiday List"):
			clear_doctype_map(master)

	def _rotation(self):
		return frappe.get_doc({"doctype": "Assignment Rule", "document_type": "HD Ticket", "rule": "Round Robin",
		                       "users": [{"user": ASHA}, {"user": RAVI}]})

	def _leave(self, user, **grain):
		frappe.get_doc({"doctype": "Tatva User Leave", "user": user, "from_date": getdate(), "to_date": getdate(), **grain}).insert()

	def test_a_ticket_rotation_skips_whoever_is_on_leave_and_waits_when_everyone_is(self):
		self.assertEqual(self._rotation().get_user({}), ASHA)
		self._leave(ASHA)
		self.assertEqual(self._rotation().get_user({}), RAVI)
		self._leave(RAVI)
		self.assertIsNone(self._rotation().get_user({}))

	def test_leave_in_one_grain_does_not_stop_tickets(self):
		self._leave(ASHA, grain_vertical=GRAIN["vertical"])
		self.assertEqual(self._rotation().get_user({}), ASHA)

	def test_a_ticket_rotation_assigns_nobody_on_a_company_holiday(self):
		holiday_list(None, getdate())
		self.assertIsNone(self._rotation().get_user({}))
