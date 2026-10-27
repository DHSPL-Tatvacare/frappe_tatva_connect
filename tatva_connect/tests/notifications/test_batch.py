# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One bulk spine: a batch's assignments are counted per person and told once per wave, never per record and never twice; only frappe's notice writer and the switch are stubbed, the redis counts are real and removed after each test."""
import time
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.lead import assignment
from tatva_connect.notifications import batch, dispatch

ACTION = "CRM List Action Job"
WRITER = "frappe.desk.doctype.notification_log.notification_log.enqueue_create_notification"
ASHA, RAVI = "zz-batch-asha@example.com", "zz-batch-ravi@example.com"


class TestOneBulkSpine(IntegrationTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		with patch("tatva_connect.tatva_connect.doctype.crm_list_action_job.crm_list_action_job.enqueue"):  # the record only, never a queued run a worker would try
			self.job = frappe.get_doc({"doctype": ACTION, "action": "Assign", "target_doctype": "CRM Lead"}).insert(ignore_permissions=True).name
		self.lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Zz Batch", "status": "New"}).insert(ignore_permissions=True).name
		self.wave = frappe.as_json([ACTION, self.job, "CRM Lead"], indent=None)
		self._clear()
		self.addCleanup(self._clear)
		self.enterContext(patch.object(dispatch, "armed", return_value=True))
		self.sent = self.enterContext(patch(WRITER))

	def _clear(self):
		batch._run(("delete", batch._key("tally", self.wave), batch._key("last", self.wave)), ("srem", batch._key("open"), self.wave))

	def _assign(self, *users):
		with batch.holding(ACTION, self.job):
			for user in users:
				batch.count(user, "CRM Lead", self.lead)

	def _age(self):
		"""The wave as it stands QUIET_SECONDS later."""
		batch._run(("setex", batch._key("last", self.wave), batch.KEEP_SECONDS, time.time() - batch.QUIET_SECONDS - 1))

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
		self.assertIn(self.job, told[ASHA]["link"])  # it opens only the leads this bulk action gave them

	def test_a_record_a_batch_assigns_notes_that_batch(self):
		self._assign(ASHA)
		self.assertEqual(frappe.db.get_value("CRM Lead", self.lead, list(batch.STAMP)), (ACTION, self.job))

	def test_a_bulk_action_stamp_never_shows_on_a_feed(self):
		from tatva_connect.activity import lead_events

		stamp_only = frappe._dict(data=frappe.as_json({"changed": [[f, None, v] for f, v in zip(batch.STAMP, (ACTION, self.job), strict=True)]}),
		                          creation=frappe.utils.now_datetime(), owner="Administrator")
		for doctype in ("CRM Lead", "CRM Task"):
			self.assertEqual(lead_events.field_changes(doctype, [stamp_only], doctype == "CRM Lead"), [], doctype)

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
		if not frappe.db.exists("User", ASHA):
			frappe.get_doc({"doctype": "User", "email": ASHA, "first_name": "Batch Asha", "send_welcome_email": 0}).insert(ignore_permissions=True)
		assignment.assign("CRM Lead", self.lead, "Administrator", notify=False)
		with patch("frappe.desk.form.assign_to.notify_assignment") as told:
			assignment.assign_for_system("CRM Lead", self.lead, ASHA, replace=True)
		self.assertEqual([c.args[1] for c in told.call_args_list], [ASHA], "only the new holder is told")
