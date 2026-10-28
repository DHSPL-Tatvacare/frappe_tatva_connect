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

	def _age(self, seconds=None):
		"""The wave as it stands `seconds` later; by default just past its quiet window."""
		seconds = batch._quiet_seconds() + 1 if seconds is None else seconds
		batch._run(("setex", batch._key("last", self.wave), batch.KEEP_SECONDS, time.time() - seconds))

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

	def test_a_slower_drain_does_not_split_a_wave(self):
		settings = frappe.get_single("Tatva Platform Settings")
		self.addCleanup(frappe.clear_document_cache, settings.doctype, settings.name)  # after the rollback, the cached copy is read fresh again
		self.addCleanup(frappe.db.rollback)
		settings.pace_interval_seconds = 120  # an operator slows the workflow drain to one pass every two minutes
		settings.save(ignore_permissions=True)
		self._assign(ASHA)
		self._age(100)  # past the old 60 s, still inside one 120 s pass
		batch.flush()
		self.sent.assert_not_called()
		self._age(136)  # past one pass and the margin
		batch.flush()
		self.assertEqual([c.args[1]["subject"] for c in self.sent.call_args_list], ["1 lead assigned to you"])

	def test_a_scheduled_cohort_tells_its_own_author_too(self):
		from tatva_connect.workflow_engine.tests import fixtures as fx

		workflow = fx.make_workflow("ZZ batch cohort author", [fx.node("start", "Trigger", edges={"next": "end"}), fx.node("end", "Terminal")],
		                            lifecycle_state="Draft").name  # inserted as Administrator, so Administrator is its author
		wave = frappe.as_json([batch.SCHEDULED, workflow, "CRM Lead"], indent=None)
		self.addCleanup(batch._run, ("delete", batch._key("tally", wave), batch._key("last", wave)), ("srem", batch._key("open"), wave))
		with batch.holding(batch.SCHEDULED, workflow):
			batch.count("Administrator", "CRM Lead", self.lead)
		batch._run(("setex", batch._key("last", wave), batch.KEEP_SECONDS, time.time() - batch._quiet_seconds() - 1))
		batch.flush()
		self.assertIn("Administrator", self._told(), "the schedule ran it, not its author, so its author is told")

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
