import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect import bulk_actions

JOB_DOCTYPE = "tatva_connect.tatva_connect.doctype.crm_list_action_job.crm_list_action_job"


class TestBulkActionsSeam(IntegrationTestCase):
	def setUp(self):
		# The row is the subject here, not the drain — a real enqueue would outlive the row this test deletes.
		enqueue = patch(f"{JOB_DOCTYPE}.enqueue")
		enqueue.start()
		self.addCleanup(enqueue.stop)

	def _queue(self, action="Assign", doctype="CRM Lead", names=("a", "b", "c"), params="{}"):
		out = bulk_actions.run_or_queue(action, doctype, json.dumps(list(names)), params)
		self.addCleanup(self._drop, out.get("job"))
		return out

	def _drop(self, job):
		if job and frappe.db.exists("CRM List Action Job", job):
			frappe.delete_doc("CRM List Action Job", job, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_one_row_queues_like_any_other(self):
		"""A single heavy lead is exactly the case that must not run in the rep's own request."""
		with patch.object(bulk_actions, "_run") as run:
			out = self._queue("Bulk Delete", names=["CRM-LEAD-0001"], params=json.dumps({"delete_linked": True}))
		run.assert_not_called()
		self.assertTrue(out["queued"])
		self.assertEqual(frappe.db.get_value("CRM List Action Job", out["job"], "status"), "Queued")

	def test_every_action_queues_whatever_the_size(self):
		for action in ("Assign", "Clear Assignment", "Reassign", "Bulk Edit", "Bulk Delete"):
			for size in (1, 25):
				params = json.dumps({"field": "status", "value": "Open"}) if action == "Bulk Edit" else "{}"
				with patch.object(bulk_actions, "_run") as run, \
				     patch.object(bulk_actions, "_assert_may_run"):  # Bulk Edit's own pre-flight needs real rows
					out = bulk_actions.run_or_queue(
						action, "CRM Lead", json.dumps([f"CRM-LEAD-{i:04d}" for i in range(size)]), params
					)
				self.addCleanup(self._drop, out.get("job"))
				self.assertTrue(out["queued"], f"{action} / {size} rows ran inline")
				run.assert_not_called()

	def test_run_or_queue_refuses_over_max_rows(self):
		docnames = json.dumps([f"CRM-LEAD-{i:04d}" for i in range(bulk_actions.MAX_ROWS + 1)])
		with self.assertRaises(frappe.ValidationError):
			bulk_actions.run_or_queue("Assign", "CRM Lead", docnames, json.dumps({"assign_to": ["Administrator"]}))

	def test_result_names_the_records_it_is_acting_on(self):
		"""A record's page reads this to know a delete is running on it."""
		out = self._queue("Bulk Delete", names=["CRM-LEAD-0001", "CRM-LEAD-0002"],
		                  params=json.dumps({"delete_linked": True}))
		result = bulk_actions.status(out["job"])
		self.assertEqual(result["target_doctype"], "CRM Lead")
		self.assertEqual(result["docnames"], ["CRM-LEAD-0001", "CRM-LEAD-0002"])

	def test_a_job_past_its_own_timeout_reads_as_failed(self):
		"""No invented ceiling: the job's enqueue timeout is the only clock, so a worker that never came
		back stops reading as 'still running' — and a slow one that finishes corrects itself."""
		from frappe.utils import add_to_date, now_datetime

		from tatva_connect.tatva_connect.doctype.crm_list_action_job.crm_list_action_job import BULK_TIMEOUT

		out = self._queue("Bulk Delete", names=["CRM-LEAD-0001"], params=json.dumps({"delete_linked": True}))
		self.assertEqual(bulk_actions.status(out["job"])["status"], "Queued")

		stale = add_to_date(now_datetime(), seconds=-(BULK_TIMEOUT + 60))
		frappe.db.set_value("CRM List Action Job", out["job"], "creation", stale, update_modified=False)
		result = bulk_actions.status(out["job"])
		self.assertEqual(result["status"], "Error")
		self.assertIn("did not finish", result["error"])
		self.assertEqual(frappe.db.get_value("CRM List Action Job", out["job"], "status"), "Queued")  # read, never written

	def test_status_carries_the_jobs_own_creation_timestamp(self):
		"""The panel sorts bulk jobs and exports by time, so `_result` carries the row's own `creation`."""
		queued = self._queue(names=[f"CRM-LEAD-{i:04d}" for i in range(25)])
		with patch.object(bulk_actions, "_run",
		                  return_value={"succeeded": 25, "failed": 0, "failed_names": []}):
			bulk_actions.run(queued["job"])
		doc = frappe.get_doc("CRM List Action Job", queued["job"])
		self.assertEqual(doc.status, "Completed", doc.error_message)

		out = bulk_actions.status(queued["job"])
		self.assertIn("creation", out)
		self.assertIsNotNone(out["creation"])
		self.assertEqual(out["creation"], doc.creation)
