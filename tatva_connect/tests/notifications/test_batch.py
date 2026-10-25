# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One bulk spine: a batch's assignments are counted per person and told once per wave, never per record and never twice; only frappe's notice writer and the switch are stubbed, the redis counts are real and removed after each test."""
import time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead import assignment
from tatva_connect.notifications import batch, dispatch

REF = "CRM List Action Job:zz-batch-probe"
WRITER = "frappe.desk.doctype.notification_log.notification_log.enqueue_create_notification"
ASHA, RAVI = "zz-batch-asha@example.com", "zz-batch-ravi@example.com"


class TestOneBulkSpine(FrappeTestCase):
	def setUp(self):
		self._clear()
		self.addCleanup(self._clear)
		self.enterContext(patch.object(dispatch, "armed", return_value=True))
		self.sent = self.enterContext(patch(WRITER))

	def _clear(self):
		batch._run(("delete", batch._key("tally", REF), batch._key("last", REF), batch._key("meta", REF)), ("srem", batch._key("open"), REF))

	def _assign(self, *users):
		with batch.holding(REF, "CRM Lead"):
			for user in users:
				batch.count(user)

	def _age(self):
		"""The wave as it stands QUIET_SECONDS later."""
		batch._run(("setex", batch._key("last", REF), batch.KEEP_SECONDS, time.time() - batch.QUIET_SECONDS - 1))

	def _told(self):
		return {c.args[0]: c.args[1] for c in self.sent.call_args_list}

	def test_a_wave_tells_each_person_once_with_their_count(self):
		self._assign(ASHA, ASHA, ASHA, RAVI)
		batch.flush()
		self.sent.assert_not_called()  # still inside its quiet window
		self._age()
		batch.flush()
		told = self._told()
		self.assertEqual(set(told), {ASHA, RAVI})
		self.assertEqual(told[ASHA]["subject"], "3 leads assigned to you")
		self.assertEqual(told[RAVI]["subject"], "1 lead assigned to you")
		self.assertIn("/crm/leads/view/list?filters=", told[ASHA]["link"])

	def test_a_wave_is_told_once_however_many_flushes_see_it(self):
		self._assign(ASHA)
		self._age()
		batch.flush()
		batch.flush()
		self.assertEqual(self.sent.call_count, 1)

	def test_a_later_burst_is_its_own_wave(self):
		self._assign(ASHA)
		self._age()
		batch.flush()
		self._assign(ASHA, ASHA)
		self._age()
		batch.flush()
		self.assertEqual([c.args[1]["subject"] for c in self.sent.call_args_list], ["1 lead assigned to you", "2 leads assigned to you"])

	def test_the_switch_off_still_tells_in_waves_never_per_record(self):
		with patch.object(dispatch, "armed", return_value=False):
			self._assign(ASHA, ASHA)
			self._age()
			batch.flush()
		self.assertEqual([c.args[1]["subject"] for c in self.sent.call_args_list], ["2 leads assigned to you"])

	def test_a_holder_moved_off_is_never_told(self):
		self.addCleanup(frappe.db.rollback)
		if not frappe.db.exists("User", ASHA):
			frappe.get_doc({"doctype": "User", "email": ASHA, "first_name": "Batch Asha", "send_welcome_email": 0}).insert(ignore_permissions=True)
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Zz Batch", "status": "New"}).insert(ignore_permissions=True).name
		assignment.assign("CRM Lead", lead, "Administrator", notify=False)
		with patch("frappe.desk.form.assign_to.notify_assignment") as told:
			assignment.assign_for_system("CRM Lead", lead, ASHA, replace=True)
		self.assertEqual([c.args[1] for c in told.call_args_list], [ASHA], "only the new holder is told")
