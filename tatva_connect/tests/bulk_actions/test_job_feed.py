"""A bulk job reports itself as it runs, and the Bulk Actions panel keeps every job the way the notification tray keeps every notice.

Run:
    bench --site dev.localhost run-tests --module tatva_connect.tests.bulk_actions.test_job_feed
"""
import random
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from tatva_connect import bulk_actions, job_feed

JOB = "CRM List Action Job"
ROW = "tatva_connect.tatva_connect.doctype.crm_list_action_job.crm_list_action_job"


class TestJobFeed(IntegrationTestCase):
	def setUp(self):
		# The run is called inline below; a real enqueue would race it.
		enqueue = patch(f"{ROW}.enqueue")
		enqueue.start()
		self.addCleanup(enqueue.stop)

	def _queue(self, names, action="Clear Assignment"):
		job = bulk_actions.run_or_queue(action, "CRM Lead", frappe.as_json(names), "{}")["job"]
		self.addCleanup(self._drop, JOB, job)
		return job

	def _drop(self, doctype, name):
		# The run commits, so the row outlives the test's rollback unless the delete commits too.
		frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _lead(self):
		doc = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Feed",
		                      "mobile_no": f"+9190{random.randint(10_000_000, 99_999_999)}"}).insert(ignore_permissions=True)
		# The run commits per row, so the test's own rollback cannot undo this lead.
		self.addCleanup(self._drop, "CRM Lead", doc.name)
		return doc.name

	def test_a_running_job_reports_each_row(self):
		"""Before this, the counts arrived once at the end, so a running job read 0 of N for minutes."""
		names = [self._lead() for _ in range(3)]
		job = self._queue(names)
		# The message pace is what keeps a real run light on the socket; at zero every step speaks, so each one can be seen.
		with patch.object(job_feed, "SAY", 0), patch.object(frappe, "publish_realtime") as published:
			bulk_actions.run(job)

		said = [c.args[1] for c in published.call_args_list if c.args[0] == job_feed.EVENT]
		running = [p["succeeded"] for p in said if p["status"] == "Started"]
		self.assertEqual(running, [0, 1, 2, 3], "the job did not report row by row while it ran")
		self.assertEqual(said[-1]["status"], "Completed")
		self.assertEqual(said[-1]["seen"], 0, "a finished job did not come back as news")
		self.assertTrue(all(c.kwargs.get("user") == frappe.session.user for c in published.call_args_list
		                    if c.args[0] == job_feed.EVENT), "a job event left its owner's room")

	def test_the_feed_keeps_every_job_and_pages_like_the_tray(self):
		"""The old window was 15 minutes and 10 rows, so the panel went empty. Nothing ages out of the feed now."""
		old, new = self._queue(["CRM-LEAD-0001"]), self._queue(["CRM-LEAD-0002"])
		frappe.db.set_value(JOB, old, "creation", add_to_date(now_datetime(), days=-30), update_modified=False)

		jobs = [r["job"] for r in job_feed.feed(limit=500)["items"]]
		self.assertIn(old, jobs, "a month-old job fell out of the panel")
		self.assertLess(jobs.index(new), jobs.index(old), "the feed is not newest first")

		page = job_feed.feed(limit=1)
		self.assertEqual(len(page["items"]), 1)
		self.assertTrue(page["has_more"], "Load more was not offered")

		before = job_feed.feed()["unseen"]
		job_feed.mark_seen(new)
		self.assertEqual(job_feed.feed()["unseen"], before, "a live job was marked seen while its worker writes it")
		job_feed.mark_seen(old)
		self.assertEqual(job_feed.feed()["unseen"], before - 1, "a job whose worker is gone could never be marked seen")
		before -= 1

		with patch.object(bulk_actions, "_run", return_value={"succeeded": 1, "failed": 0, "failed_names": []}), \
		     patch.object(frappe, "publish_realtime"):
			bulk_actions.run(new)
		job_feed.mark_seen(new)
		self.assertEqual(job_feed.feed()["unseen"], before - 1, "opening a finished job did not mark it seen")

	def test_abort_closes_a_live_job_for_its_owner_only(self):
		"""A hard stop: the row ends Aborted, a worker that reaches it later does nothing, and nobody else can abort it."""
		lead = self._lead()
		job = self._queue([lead], action="Assign")
		frappe.db.set_value(JOB, job, "params", frappe.as_json({"assign_to": ["Administrator"]}))

		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				job_feed.abort(job)
		finally:
			frappe.set_user("Administrator")

		with patch.object(frappe, "publish_realtime"):
			out = job_feed.abort(job)
		self.assertTrue(out["aborted"], "the job was not closed as aborted")
		self.assertFalse(out["abortable"], "an aborted job still offers Abort")

		bulk_actions.run(job)  # the worker reaching it after the abort
		self.assertEqual(frappe.db.get_value(JOB, job, "succeeded"), 0, "an aborted job still ran")
		self.assertFalse(frappe.db.exists("ToDo", {"reference_name": lead, "status": "Open"}), "an aborted job still assigned")
